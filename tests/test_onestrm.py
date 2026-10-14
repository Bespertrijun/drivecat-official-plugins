"""Queue and HTTP acceptance tests, without a running DriveCat or OneStrm service."""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

PLUGIN = Path(__file__).resolve().parents[1] / "plugins/onestrm"
sys.path.insert(0, str(PLUGIN))
from onestrm_client import Result, notify, probe_network, retry_after
from onestrm_config import (
    Config,
    cloud_path,
    load_config,
    public_config,
    target_key,
    write_config,
)
from onestrm_dispatcher import Dispatcher
from onestrm_state import State


def configuration(**overrides):
    return {**Config(enabled=True, drive_id=1, base_url="http://onestrm:8080", token="test-secret").model_dump(exclude={"clear_token"}),
            "changed_at": 1000, "revision": 1, "auth_revision": 0, "drive_name": "115 A", **overrides}


def task(task_id, completed=1001, path="/影视/某剧/01.mkv", drive=1, status="success"):
    return {"id": task_id, "remote_path": path, "drive_config_id": drive, "status": status,
            "completed_at": datetime.fromtimestamp(completed, timezone.utc)}


class FakeHost:
    def __init__(self, rows=()):
        self.rows = {r["id"]: r for r in rows}

    def by_ids(self, ids):
        return {i: self.rows[i] for i in ids if i in self.rows}


class Fixture:
    def setup_state(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = configuration()
        write_config(self.temp.name, self.config)
        self.state = State(self.temp.name)
        self.key = target_key(self.config)
        self.state.status()

    def settings(self, now, **overrides):
        self.config = {**self.config, **overrides, "changed_at": now, "revision": self.config["revision"] + 1}
        write_config(self.temp.name, self.config)
        self.state.status()

    def enqueue(self, task_id=1, now=1001, path="/影视/某剧"):
        with self.state.connect(write=True) as db:
            return self.state.enqueue(db, self.key, task_id, path, now, 10)


class StateTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_state()

    def test_remote_paths_and_invalid_paths(self):
        for value, parent in [("/影视/某剧/01.mkv", "/影视/某剧"), ("/影视/a.mkv", "/影视"),
                              ("/a.mkv", "/"), ("/影视/目录 空格/字幕.ass", "/影视/目录 空格")]:
            self.assertEqual(cloud_path(value, file=True), parent)
        for value in ["", None, "relative/a.mkv", "/", "/影视/", "/影视/.", "/a/../b", "//a/b", "/a\nb"]:
            with self.assertRaises(ValueError):
                cloud_path(value, file=True)
        self.assertEqual(cloud_path("/"), "/")

    def test_atomic_config_failure_keeps_previous_and_hides_token(self):
        with patch("onestrm_config.os.replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            write_config(self.temp.name, {**self.config, "token": "new"})
        self.assertEqual(load_config(self.temp.name)["token"], "test-secret")
        self.assertNotIn("test-secret", json.dumps(public_config(self.config)))
        self.assertEqual((Path(self.temp.name) / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), ["config.json", "state.sqlite3"])

    def test_ten_files_one_batch_and_no_permanent_file_marker(self):
        for i in range(1, 11):
            self.enqueue(i, now=1000 + i)
        self.assertIsNone(self.state.claim(1019))
        claimed = self.state.claim(1020)
        self.assertIsNotNone(claimed)
        self.assertEqual(self.state.status()["rows"][0]["file_count"], 10)
        self.state.finish(claimed, Result(True, http_status=202), 1021)
        self.assertEqual(self.state.status()["pending_count"], 0)
        self.assertEqual(self.state.status()["history"][0]["file_count"], 10)
        with self.state.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)
        self.enqueue(1, now=1022)  # Same upload is allowed again after completion.
        self.assertIsNotNone(self.state.claim(1032))

    def test_quiet_window_has_maximum_wait(self):
        for i in range(1, 80):
            self.enqueue(i, now=1000 + i)
        self.assertEqual(self.state.status()["rows"][0]["due"], 1061)

    def test_request_in_flight_keeps_new_files_and_excludes_other_claimants(self):
        self.enqueue(1)
        first = self.state.claim(1011)
        self.assertIsNone(State(self.temp.name).claim(1012))
        self.enqueue(2, now=1012)
        self.state.finish(first, Result(True), 1013)
        self.assertEqual(self.state.status()["pending_count"], 1)
        self.assertEqual(self.state.status()["rows"][0]["file_count"], 1)
        second = self.state.claim(1022)
        self.assertNotEqual(first["id"], second["id"])

    def test_concurrent_hooks_merge_and_filter_accounts(self):
        def capture(i):
            State(self.temp.name).capture({"upload_task_id": i, "drive_config_id": 1, "remote_path": "/a"}, now=1001)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(capture, [1, 2, 3, 1, 2, 4]))
        self.state.capture({"upload_task_id": 8, "drive_config_id": 2}, now=1001)
        self.assertEqual(len(self.state.clues(self.key)), 4)

    def test_retry_budget_survives_duplicates_and_restarts(self):
        batch = self.enqueue()
        due = 1011
        for attempt, delay in enumerate([30, 120, 600, 600], start=1):
            claim = State(self.temp.name).claim(due)
            self.assertEqual(claim["attempts"], attempt)
            self.state.finish(claim, Result(False, "server", "HTTP 503", 503, True), due)
            original = self.state.status()["rows"][0]
            self.enqueue(1, now=due + 1)
            self.enqueue(attempt + 10, now=due + 1)
            row = self.state.status()["rows"][0]
            self.assertEqual((row["attempts"], row["due"]), (attempt, original["due"]))
            due += delay
        self.assertEqual(self.state.status()["rows"][0]["status"], "exhausted")
        self.assertIsNone(self.state.claim(due + 1000))
        self.state.retry(batch, due + 1001)
        self.assertEqual(self.state.claim(due + 1001)["attempts"], 1)

    def test_auth_pause_and_credential_repair(self):
        self.enqueue()
        self.state.finish(self.state.claim(1011), Result(False, "auth", "HTTP 401", 401), 1011)
        self.enqueue(2, path="/another", now=1012)
        self.assertTrue(self.state.status()["paused"])
        self.assertIsNone(self.state.claim(1050))
        self.settings(1051, token="repaired", auth_revision=1)
        self.assertFalse(self.state.status()["paused"])
        self.assertIsNotNone(self.state.claim(1051))

    def test_rate_limit_applies_to_all_directories(self):
        self.enqueue()
        claim = self.state.claim(1011)
        self.state.finish(claim, Result(False, "rate_limit", "HTTP 429", 429, True, 2000), 1011)
        self.enqueue(2, path="/another", now=1012)
        self.assertIsNone(self.state.claim(1999))
        self.assertIsNotNone(self.state.claim(2000))

    def test_expired_lease_recovers_but_stale_result_cannot_finish_new_attempt(self):
        self.enqueue()
        first = self.state.claim(1011)
        self.assertIsNone(self.state.claim(1050))
        second = State(self.temp.name).claim(1051)
        self.assertEqual(second["attempts"], 2)
        self.state.finish(first, Result(True), 1052)
        self.assertEqual(self.state.status()["pending_count"], 1)
        self.state.finish(second, Result(True), 1053)
        self.assertEqual(self.state.status()["pending_count"], 0)

    def test_disable_and_switch_do_not_redirect_old_work(self):
        self.enqueue()
        self.settings(1002, enabled=False)
        self.assertIsNone(self.state.claim(1020))
        self.settings(1021, enabled=True, drive_id=2, base_url="http://another")
        self.assertIsNone(self.state.claim(1050))
        self.assertTrue(self.state.status()["rows"][0]["config_changed"])
        self.settings(1051, drive_id=1, base_url="http://onestrm:8080")
        self.assertIsNotNone(self.state.claim(1052))

    def test_cancel_allows_later_repeat(self):
        batch = self.enqueue()
        self.state.cancel("batch", batch, 1002)
        self.assertEqual(self.state.status()["history"][0]["status"], "cancelled")
        self.enqueue(1, now=1003)
        self.assertIsNotNone(self.state.claim(1013))

    def test_capture_ignores_events_while_disabled(self):
        self.settings(1002, enabled=False)
        self.state.capture({"upload_task_id": 1, "drive_config_id": 1, "remote_path": "/影视/某剧/01.mkv"}, now=1003)
        self.assertEqual(self.state.clues(self.key), [])

    def test_crash_between_config_rename_and_state_commit_recovers_settings(self):
        updated = {**self.config, "enabled": False, "changed_at": 1003, "revision": 2}
        write_config(self.temp.name, updated)
        # No sync_config call: simulate the process dying immediately after the atomic rename.
        resumed = State(self.temp.name)
        self.assertFalse(resumed.status()["enabled"])

    def test_retention_and_clear_preserve_unfinished(self):
        self.enqueue()
        with self.state.connect(write=True) as db:
            sql = "INSERT INTO history(target,path,status,file_count,attempts,updated,error) VALUES (?,'/a','success',1,1,?,'')"
            db.execute(sql, (self.key, 1))
            db.executemany(sql, [(self.key, 4_000_000 + i) for i in range(10002)])
        self.state.maintain(4_020_000)
        self.assertEqual(self.state.status()["history_count"], 10000)
        self.assertEqual(self.state.status()["pending_count"], 1)
        self.state.clear_history()
        self.assertEqual(self.state.status()["history_count"], 0)
        self.assertEqual(self.state.status()["pending_count"], 1)


class DispatcherTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_state()
        self.now = 1010
        self.host = FakeHost()
        self.sent = []

        async def sender(config, path):
            self.sent.append((config["drive_id"], path))
            return Result(True, http_status=200)

        self.dispatcher = Dispatcher(self.state, self.host, sender, clock=lambda: self.now)

    async def test_clue_delivery_filters_other_accounts_failures_and_skips(self):
        rows = [task(1), task(2, drive=2, path="/其他/b.mkv"), task(3, status="failed", path="/失败/c.mkv"),
                task(4, status="skipped", path="/跳过/d.mkv"), task(6, path="/电影/a.mkv")]
        self.host.rows = {r["id"]: r for r in rows}
        for row in rows:
            self.state.capture({"upload_task_id": row["id"], "drive_config_id": 1,
                                "remote_path": row["remote_path"]}, now=1002)
        await self.dispatcher.run()
        self.now = 1020
        await self.dispatcher.run()
        await self.dispatcher.run()
        self.assertEqual(sorted(self.sent), [(1, "/影视/某剧"), (1, "/电影")])
        self.assertEqual(self.state.status()["pending_count"], 0)

    async def test_completed_task_is_never_notified_twice(self):
        self.host.rows = {1: task(1)}
        self.state.capture({"upload_task_id": 1, "drive_config_id": 1, "remote_path": "/影视/某剧/01.mkv"}, now=1002)
        await self.dispatcher.run()
        self.now = 1020
        await self.dispatcher.run()
        self.assertEqual(self.sent, [(1, "/影视/某剧")])
        # Ingestion is hook-only; later ticks never revisit the completed upload.
        for now in (1200, 1300, 1400):
            self.now = now
            await self.dispatcher.run()
        self.assertEqual(len(self.sent), 1)

    async def test_hook_verification_missing_records_invalid_path_and_future_time(self):
        self.host.rows = {1: task(1, path=""), 2: task(2, status="failed"), 3: task(3, drive=2),
                          5: task(5, completed=9999)}
        for i in [1, 2, 3, 4, 5]:
            self.state.capture({"upload_task_id": i, "drive_config_id": 1, "remote_path": ""}, now=1002)
        await self.dispatcher.run()
        rows = self.state.status()["rows"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["error"] and r["kind"] == "inbox" for r in rows))
        self.assertEqual(self.sent, [])

    async def test_clue_consume_rolls_back_on_insert_failure(self):
        self.host.rows = {1: task(1)}
        self.state.capture({"upload_task_id": 1, "drive_config_id": 1, "remote_path": "/影视/某剧/01.mkv"}, now=1002)
        with patch.object(self.state, "enqueue", side_effect=RuntimeError("disk failure")):
            await self.dispatcher.run()
        self.assertEqual(len(self.state.clues(self.key)), 1)
        self.assertTrue(self.state.status()["error"])
        self.now = 1020
        await self.dispatcher.run()
        self.assertFalse(self.state.status()["error"])
        self.now = 1030
        await self.dispatcher.run()
        self.assertEqual(self.sent, [(1, "/影视/某剧")])

    async def test_unload_drains_request_and_stops_new_claims(self):
        started, finish = asyncio.Event(), asyncio.Event()
        self.enqueue()
        self.now = 1020

        async def slow(config, path):
            started.set()
            await finish.wait()
            return Result(True)

        self.dispatcher.sender = slow
        running = asyncio.create_task(self.dispatcher.run())
        await started.wait()
        closing = asyncio.create_task(self.dispatcher.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        finish.set()
        await running
        await closing
        self.enqueue(2, now=1021)
        self.now = 1040
        await self.dispatcher.run()
        self.assertEqual(self.state.status()["pending_count"], 1)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.code, self.extra_headers, self.delay = [], 200, {}, 0

        async def handler(request):
            self.calls.append({"path": request.path, "query": dict(request.query), "headers": dict(request.headers),
                               "json": await request.json()})
            if self.delay:
                await asyncio.sleep(self.delay)
            return web.Response(status=self.code, headers=self.extra_headers, text='{"success":false,"token":"echoed-secret"}')

        app = web.Application()
        app.router.add_post('/prefix/webhook/quick_scan', handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.config = configuration(base_url=f"http://127.0.0.1:{port}/prefix")

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_protocol_bearer_query_no_proxy_and_any_2xx(self):
        for code in [200, 202, 204, 299]:
            self.code = code
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
                result = await notify(self.config, "/影视/目录 空格")
            self.assertTrue(result.ok)  # Explicit user decision: ignore business body, even success:false.
            self.assertNotIn("echoed-secret", repr(result))
        first = self.calls[0]
        self.assertEqual(first["json"], {"paths": ["/影视/目录 空格"]})
        self.assertEqual(first["headers"]["Authorization"], "Bearer test-secret")
        self.assertNotIn("Cookie", first["headers"])
        self.config["auth_mode"] = "query"
        await notify(self.config, "/")
        self.assertEqual(self.calls[-1]["query"], {"token": "test-secret"})
        self.assertNotIn("Authorization", self.calls[-1]["headers"])

    async def test_failures_and_no_redirects(self):
        for code, kind, retry in [(401, "auth", False), (403, "auth", False), (404, "http", False),
                                   (429, "rate_limit", True), (500, "server", True), (503, "server", True),
                                   (302, "http", False)]:
            self.code, self.extra_headers = code, {"Retry-After": "180", "Location": self.config["base_url"]}
            before = len(self.calls)
            result = await notify(self.config, "/a")
            self.assertEqual((result.ok, result.kind, result.retryable), (False, kind, retry))
            self.assertEqual(len(self.calls), before + 1)
            if code == 429:
                self.assertGreater(result.retry_at, time.time() + 170)

    async def test_timeout_and_tcp_probe_does_not_scan(self):
        self.assertTrue((await probe_network(self.config["base_url"]))["ok"])
        self.assertEqual(self.calls, [])
        self.delay = 1.5
        result = await notify({**self.config, "timeout_seconds": 0.05}, "/a")
        self.assertEqual(result.kind, "timeout")
        self.assertTrue(result.retryable)

    async def test_network_failure_and_retry_after_date(self):
        await self.site.stop()
        result = await notify(self.config, "/a")
        self.assertEqual(result.kind, "network")
        self.assertTrue(result.retryable)
        self.assertEqual(retry_after("Thu, 01 Jan 1970 00:30:00 GMT", 1000), 1800)
        for value in ["invalid", "nan", "inf", ""]:
            self.assertIsNone(retry_after(value, 1000))


if __name__ == "__main__":
    unittest.main()
