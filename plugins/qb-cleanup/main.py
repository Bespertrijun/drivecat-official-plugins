"""Use existing host DB and scheduler APIs for watcher-aware qB cleanup."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.plugin.base import PluginInterface, PluginMeta

_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from qb_cleanup import QbClient, QbError, absolute_path, mapped_path, torrent_files
from qb_monitor import Monitor, TASK_FIELDS
from qb_state import State
from datetime import datetime, timezone


_active_monitor = None


async def scheduled_poll():
    # APScheduler persists callable references: do not pickle a plugin with locks/DB context.
    monitor = _active_monitor
    if monitor is not None:
        await asyncio.to_thread(monitor.run)


class PathMapping(BaseModel):
    local_path: str
    qb_path: str

    @field_validator("local_path", "qb_path")
    @classmethod
    def valid_path(cls, value):
        return absolute_path(value)


class Config(BaseModel):
    enabled: bool = False
    base_url: str = ""
    username: str = ""
    password: str | None = None
    timeout_seconds: int = Field(default=5, ge=1, le=10)
    watcher_ids: list[int] = Field(default_factory=list)
    poll_seconds: int = Field(default=10, ge=5, le=300)
    quiet_seconds: int = Field(default=60, ge=1, le=86400)
    path_mappings: list[PathMapping] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("请填写 http(s) WebUI 地址，账号密码请单独填写")
        try:
            parsed.port
        except ValueError:
            raise ValueError("端口无效") from None
        return value

    @model_validator(mode="after")
    def valid_config(self):
        sources = [m.local_path for m in self.path_mappings]
        if len(sources) != len(set(sources)):
            raise ValueError("DriveCat 映射路径不能重复")
        if any(i <= 0 for i in self.watcher_ids) or len(set(self.watcher_ids)) != len(self.watcher_ids):
            raise ValueError("watcher ID 必须是唯一正整数")
        if self.enabled and (not self.base_url or not self.path_mappings or not self.watcher_ids):
            raise ValueError("启用前请填写 qB 地址、路径映射并选择 watcher")
        return self


def load_config(directory):
    path = Path(directory) / "config.json"
    value = json.loads(path.read_text()) if path.exists() else {}
    if "watcher_ids" not in value:
        value["enabled"] = False
    config = Config.model_validate(value).model_dump()
    config["password"] = config["password"] or ""
    return config


def public_config(config):
    return {**{k: v for k, v in config.items() if k != "password"}, "password_set": bool(config["password"])}


def host_snapshot(context, watcher_ids=None):
    from app.models.watch import WatchRule, UploadTarget, UploadTask

    def record(row, fields):
        result = {}
        for field in fields:
            value = getattr(row, field, None)
            if isinstance(value, datetime):
                value = value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
            result[field] = value
        return result

    db = context.get_db()
    try:
        watchers = [record(w, ('id', 'name', 'local_path', 'is_enabled', 'post_action',
                               'exclude_patterns', 'delete_excluded', 'existing_files_policy', 'updated_at'))
                    for w in db.query(WatchRule).all()]
        targets = [record(t, ('id', 'watch_rule_id', 'is_enabled', 'target_type',
                              'drive_config_id', 'balance_rule_id', 'remote_path'))
                   for t in db.query(UploadTarget).all()]
        tasks = []
        for wid in watcher_ids or []:
            cursor = 0
            while True:
                rows = (db.query(UploadTask).filter(UploadTask.watch_rule_id == wid, UploadTask.id > cursor)
                        .order_by(UploadTask.id).limit(500).all())
                if not rows:
                    break
                tasks.extend(record(t, TASK_FIELDS) for t in rows)
                cursor = rows[-1].id
        return dict(watchers=watchers, targets=targets, tasks=tasks)
    finally:
        db.close()


class QbCleanupPlugin(PluginInterface):
    def __init__(self):
        self._meta = PluginMeta(**json.loads((Path(__file__).parent / "manifest.json").read_text()))
        self.monitor = None

    def get_meta(self):
        return self._meta

    async def on_load(self, context):
        global _active_monitor
        directory = context.get_fs().root
        # Migrate the trial hook configuration to disabled if watcher selection is absent.
        self.config = load_config(directory)
        state = State(directory)
        self.monitor = Monitor(state, lambda: host_snapshot(context, self.config['watcher_ids']), lambda: self.config)
        monitor = self.monitor
        _active_monitor = monitor
        router = APIRouter()

        def merged(body):
            config = body.model_dump()
            if config["password"] is None:
                config["password"] = self.config["password"]
            return config

        @router.get("/config")
        async def get_config():
            return {"config": public_config(self.config)}

        @router.post("/config")
        async def save_config(body: Config):
            def save():
                with monitor.lock:
                    config = merged(body)
                    name = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as file:
                            name = file.name
                            json.dump(config, file, ensure_ascii=False, indent=2)
                            file.flush()
                            os.fsync(file.fileno())
                        os.replace(name, Path(directory) / "config.json")
                    finally:
                        if name and os.path.exists(name):
                            os.unlink(name)
                    self.config = config
                    monitor.invalidate()
                    return {"ok": True, "config": public_config(config)}
            return await asyncio.to_thread(save)

        @router.get("/watchers")
        async def watchers():
            return {"watchers": (await asyncio.to_thread(host_snapshot, context))['watchers']}

        @router.get("/status")
        async def status(limit: int = 100, offset: int = 0):
            return await asyncio.to_thread(monitor.status, max(1, min(limit, 100)), max(0, offset))

        @router.post("/check")
        async def check():
            await asyncio.to_thread(monitor.run, True)
            return await asyncio.to_thread(monitor.status)

        @router.post("/cancel/{key}")
        async def cancel(key: str):
            try:
                await asyncio.to_thread(monitor.cancel, key)
            except KeyError:
                raise HTTPException(404, "任务不存在") from None
            return {"ok": True}

        @router.post("/history/clear")
        async def clear():
            def clear_history():
                with monitor.lock:
                    state.clear_completed()
            await asyncio.to_thread(clear_history)
            return {"ok": True}

        @router.post("/mapping/check")
        async def check_mapping():
            def inspect():
                with monitor.lock:
                    config = self.config
                    if not config['base_url']:
                        return {'ok': False, 'error': '请先保存 qB 地址'}
                    host = host_snapshot(context, config['watcher_ids'])
                    client = QbClient(config)
                    client.login()
                    # Bound diagnostics independently from the deletion monitor.
                    torrents = client.json('torrents/info')
                    paths = set()
                    for torrent in torrents[:40]:
                        paths.update(torrent_files(torrent, client.json('torrents/files', {'hash': torrent['hash']})))
                    samples = []
                    for task in host['tasks'][:100]:
                        path = mapped_path(task['local_path'], config['path_mappings'])
                        samples.append(dict(local_path=task['local_path'], qb_path=path, matched=path in paths))
                    return {'ok': True, 'samples': samples, 'partial': len(torrents) > 40 or len(host['tasks']) > 100}
            try:
                return await asyncio.to_thread(inspect)
            except (QbError, ValueError) as exc:
                return {'ok': False, 'error': str(exc)}

        @router.post("/test")
        async def test_connection(body: Config):
            config = merged(body)
            if not config["base_url"]:
                raise HTTPException(422, "请填写 qB 地址")
            def probe():
                client = QbClient(config)
                client.login()
                return client.request("app/version").strip()
            try:
                return {"ok": True, "version": await asyncio.to_thread(probe)}
            except QbError as exc:
                return {"ok": False, "error": str(exc)}

        context.register_router(router, prefix="/qb-cleanup", tags=["qB 后处理"])
        context.register_job('monitor', scheduled_poll, trigger_type='interval', trigger_args={'seconds': 5}, name='qB 关联任务检查')

    async def on_unload(self):
        global _active_monitor
        if _active_monitor is self.monitor:
            _active_monitor = None
        if self.monitor:
            self.monitor.stopped.set()
            def drain():
                with self.monitor.lock:
                    self.monitor.invalidate()
            await asyncio.to_thread(drain)
