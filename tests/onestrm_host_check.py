"""Run separately with DriveCat's Python environment; uses real host APIs and temporary DBs.

DRIVECAT_BACKEND=/path/to/DriveCat/backend python tests/onestrm_host_check.py -v
"""

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(os.environ.get("DRIVECAT_BACKEND", ROOT.parent / "DriveCat/backend"))
sys.path.insert(0, str(BACKEND))

import httpx
from aiohttp import web
from app.core import scheduler as host_scheduler
from app.models.drive import DriveConfig
from app.models.user import Base
from app.models.watch import UploadTask
from app.plugin.base import HookContext, PluginContext
from app.plugin.hooks import HookDispatcher
from app.plugin.sandbox import PluginSandbox
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = load_module("drivecat_plugin_onestrm_test", ROOT / "plugins/onestrm/main.py")


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.engine = create_engine(f"sqlite:///{self.directory / 'host.sqlite3'}")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as db:
            db.add_all([DriveConfig(id=1, name="115 A", drive_type="115", config_data={"cookie": "private-cookie"}, is_active=True),
                        DriveConfig(id=2, name="115 B", drive_type="115", config_data={}, is_active=True),
                        DriveConfig(id=3, name="其他盘", drive_type="local", config_data={}, is_active=True),
                        DriveConfig(id=4, name="已禁用", drive_type="115", config_data={}, is_active=False)])
            db.commit()
        self.calls = []
        self.received = asyncio.Event()
        self.code = 200

        async def quick_scan(request):
            self.calls.append((request.path, await request.json(), request.headers.get("Authorization")))
            self.received.set()
            return web.Response(status=self.code, text="accepted")

        service = web.Application()
        service.router.add_post("/webhook/quick_scan", quick_scan)
        self.runner = web.AppRunner(service)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.scheduler = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{self.directory / 'jobs.sqlite3'}")})
        self.scheduler.start(paused=True)
        host_scheduler._scheduler = self.scheduler
        self.sandbox = PluginSandbox(max_workers=1)
        self.sandbox.set_plugin_specs([(plugin.__name__, plugin.__file__, str(ROOT / "plugins/onestrm"))])
        self.hooks = HookDispatcher()
        self.hooks.set_sandbox(self.sandbox)
        self.app = FastAPI()
        self.instance = plugin.OneStrmPlugin()
        self.context = PluginContext("onestrm-test", self.hooks, self.instance.get_meta().permissions,
                                     logging.getLogger(__name__), self.factory, self.app, self.directory / "data")
        self.hooks.set_plugins_ref({"onestrm-test": (self.instance.get_meta(), self.instance, True)})
        await self.instance.on_load(self.context)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://host")
        self.prefix = "/api/plugins/onestrm-test/onestrm"
        self.body = {"enabled": True, "drive_id": 1, "base_url": self.url, "token": "secret", "quiet_seconds": 1}
        response = await self.client.post(self.prefix + "/config", json=self.body)
        self.assertEqual(response.status_code, 200, response.text)

    async def asyncTearDown(self):
        await self.instance.on_unload()
        self.context.unregister_jobs()
        self.scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        host_scheduler._scheduler = None
        await self.sandbox.shutdown()
        await self.client.aclose()
        await self.runner.cleanup()
        self.engine.dispose()

    def add_task(self, task_id=1, drive=1, path="/影视/某剧/01.mkv", completed=None, status="success"):
        with self.factory() as db:
            db.add(UploadTask(id=task_id, watch_rule_id=1, drive_config_id=drive,
                              local_path="/missing-local-file.mkv", remote_path=path, status=status,
                              completed_at=completed or datetime.now(timezone.utc)))
            db.commit()

    async def event(self, task_id=1, drive=1, path="/影视/某剧/01.mkv"):
        result = await self.hooks.dispatch("after_upload", HookContext(hook_name="after_upload", data={
            "upload_task_id": task_id, "drive_config_id": drive, "remote_path": path, "filename": "01.mkv"}))
        self.assertTrue(result.success)

    async def test_real_sandbox_scheduler_http_and_reload(self):
        self.add_task()
        await self.event()
        self.assertEqual(len(self.instance.dispatcher.state.clues(plugin.target_key(plugin.load_config(self.context.get_fs().root)))), 1)
        self.scheduler.resume()
        await asyncio.wait_for(self.received.wait(), 10)
        async with self.instance.dispatcher.lock:
            pass
        self.scheduler.pause()
        status = (await self.client.get(self.prefix + "/status")).json()
        self.assertEqual(status["history"][0]["status"], "success")
        self.assertEqual(self.calls[0], ("/webhook/quick_scan", {"paths": ["/影视/某剧"]}, "Bearer secret"))
        with self.factory() as db:
            self.assertEqual(db.get(UploadTask, 1).status, "success")
        self.add_task(2, path="/电影/new.mkv")
        await self.event(2, path="/电影/new.mkv")
        await self.instance.on_unload()
        self.context.unregister_jobs()
        self.hooks.unregister("after_upload", "onestrm-test")
        self.instance = plugin.OneStrmPlugin()
        self.hooks.set_plugins_ref({"onestrm-test": (self.instance.get_meta(), self.instance, True)})
        await self.instance.on_load(self.context)
        await self.instance.dispatcher.run()
        # Advance only plugin clock; scheduler storage and HTTP are real.
        self.instance.dispatcher.clock = lambda: time.time() + 2
        await self.instance.dispatcher.run()
        self.assertEqual(self.calls[-1][1], {"paths": ["/电影"]})

    async def test_real_readonly_database_keyset_pages_and_success_filter(self):
        started = plugin.load_config(self.context.get_fs().root)["changed_at"]
        completed = datetime.fromtimestamp(started + 1, timezone.utc)
        with self.factory() as db:
            db.add_all([UploadTask(id=i, watch_rule_id=1, drive_config_id=1, local_path="/gone",
                                   remote_path=f"/影视/剧集/{i}.mkv", status="success", completed_at=completed)
                        for i in range(2, 1103)])
            db.commit()
        self.add_task(2001, drive=2, completed=completed)
        self.add_task(2002, status="skipped", completed=completed)
        self.add_task(2003, status="failed", completed=completed)
        dispatcher = self.instance.dispatcher
        dispatcher.clock = lambda: started + 10
        await dispatcher.run()
        self.assertEqual(dispatcher.state.status()["rows"][0]["file_count"], 1000)
        await dispatcher.run()
        self.assertEqual(dispatcher.state.status()["rows"][0]["file_count"], 1101)
        self.add_task(1, completed=datetime.fromtimestamp(started + 70, timezone.utc), path="/晚完成/late.mkv")
        dispatcher.clock = lambda: started + 80
        await dispatcher.run()
        await dispatcher.run()
        self.assertTrue(any(r["path"] == "/晚完成" and r["file_count"] == 1 for r in dispatcher.state.status()["rows"]))
        db = self.context.get_db()
        try:
            with self.assertRaises(PermissionError):
                db.commit()
        finally:
            db.close()

    async def test_api_token_masking_validation_and_saved_target_scan(self):
        drives = await self.client.get(self.prefix + "/drives")
        self.assertNotIn("private-cookie", drives.text)
        self.assertEqual([d["id"] for d in drives.json()["drives"]], [1, 2, 4])
        for token in [None, ""]:
            response = await self.client.post(self.prefix + "/config", json={**self.body, "token": token})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("secret", response.text)
            self.assertTrue(response.json()["config"]["token_set"])
        for invalid in [{"drive_id": 3}, {"drive_id": 4}, {"drive_id": 99},
                        {"base_url": "http://user:secret@host"}, {"token": "secret\n"}, {"timeout_seconds": 0}]:
            response = await self.client.post(self.prefix + "/config", json={**self.body, **invalid})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("secret", response.text)
        response = await self.client.post(self.prefix + "/test-connection", json=self.body)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.calls, [])
        response = await self.client.post(self.prefix + "/test-scan", json={"path": "/影视/测试"})
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.calls[-1][1], {"paths": ["/影视/测试"]})
        response = await self.client.post(self.prefix + "/config", json={**self.body, "clear_token": True})
        self.assertFalse(response.json()["config"]["token_set"])

    async def test_failed_notification_preserves_host_success_and_pauses_auth(self):
        self.code = 401
        self.add_task()
        await self.event()
        await self.instance.dispatcher.run()
        self.instance.dispatcher.clock = lambda: time.time() + 2
        await self.instance.dispatcher.run()
        status = self.instance.dispatcher.state.status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["rows"][0]["status"], "auth")
        with self.factory() as db:
            row = db.get(UploadTask, 1)
            self.assertEqual((row.status, row.retry_count), ("success", 0))
        response = await self.client.post(self.prefix + "/config", json={**self.body, "token": "repaired"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.instance.dispatcher.state.status()["paused"])


class PackagingTests(unittest.TestCase):
    def test_real_packager_in_disposable_git_repository(self):
        build = load_module("onestrm_build_test", ROOT / "scripts/build.py")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "plugins/onestrm", root / "plugins/onestrm", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "plugins/_shared", root / "plugins/_shared")
            for args in [["init", "-q"], ["add", "plugins"],
                         ["-c", "user.name=Local Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
                         ["tag", "onestrm/v0.0.0"]]:
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            build.ROOT = root
            package = build._pack_zip_for_tag("onestrm", "0.0.0", "onestrm/v0.0.0", root / "out")
            with zipfile.ZipFile(package) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["version"], "0.0.0")
                self.assertEqual(manifest["name"], "OneStrm 自动工具")
                for name in ["main.py", "onestrm_config.py", "onestrm_client.py", "onestrm_dispatcher.py",
                             "onestrm_state.py", "ui/index.html", "ui/app.js", "ui/style.css", "_shared/sdk.js"]:
                    self.assertIn(name, archive.namelist())
                self.assertFalse(any("__pycache__" in name or name.endswith(".sqlite3") for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
