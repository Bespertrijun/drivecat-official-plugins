"""OneStrm 自动工具: host hooks, scheduler and plugin UI APIs."""

import asyncio
import functools
import json
import logging
import sys
import time
from pathlib import Path
from typing import Literal

from app.plugin.base import PluginInterface, PluginMeta
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

_plugin_dir = Path(__file__).resolve().parent
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))
# Follow notify's hot-reload pattern without touching other plugins' modules.
for _name, _module in list(sys.modules.items()):
    _file = getattr(_module, "__file__", None)
    if _name.startswith("onestrm_") and _file and Path(_file).resolve().parent == _plugin_dir:
        sys.modules.pop(_name, None)

from onestrm_client import probe_network
from onestrm_config import (
    Config,
    cloud_path,
    load_config,
    public_config,
    target_key,
    write_config,
)
from onestrm_dispatcher import Dispatcher, HostTasks
from onestrm_state import State

_active_dispatcher = None


def after_upload(directory, ctx):
    """Synchronous, pickleable hook, as required by the real host process sandbox."""
    try:
        if ctx.hook_name == "after_upload":
            State(directory).capture(ctx.data)
    except Exception:  # noqa: BLE001 - isolate plugin failures from the completed upload
        # Upload success is already committed. Periodic read-only reconciliation repairs a missed hook.
        logging.getLogger(__name__).warning("OneStrm 事件交接失败，将由成功任务补查尝试恢复")


async def scheduled_poll():
    dispatcher = _active_dispatcher
    if dispatcher is not None:
        await dispatcher.run()


def validate_config(body):
    try:
        return Config.model_validate(body)
    except ValidationError:
        # FastAPI's default validation response includes the rejected input (possibly a token).
        raise HTTPException(422, "设置无效，请检查服务地址、Token、数值范围及启用必填项") from None


def save_settings(state, body, drives, now):
    with state.connect(write=True) as db:
        previous = state.current(db)
        config = body.model_dump(exclude={"clear_token"})
        config["token"] = "" if body.clear_token else (body.token or previous["token"])
        selected = next((d for d in drives if d["id"] == config["drive_id"]), None)
        valid_drive = selected and selected["drive_type"] == "115" and selected["is_active"]
        if not valid_drive and (config["enabled"] or
                                (config["drive_id"] is not None and config["drive_id"] != previous["drive_id"])):
            raise ValueError("请选择一个已启用的 115 网盘")
        if selected and selected["drive_type"] != "115":
            raise ValueError("只能选择 115 网盘")
        if now < previous["changed_at"]:
            raise ValueError("系统时间发生回退，请恢复时间后保存")
        config.update(changed_at=now, revision=previous["revision"] + 1,
                      auth_revision=previous["auth_revision"] + int(
                          (config["token"], config["auth_mode"]) != (previous["token"], previous["auth_mode"])),
                      drive_name=selected["name"] if selected else previous["drive_name"])
        # Same SQLite writer lock as capture(). A crash after the rename is repaired by current().
        write_config(state.directory, config)
        state.sync_config(db, config)
        return public_config(config)


class OneStrmPlugin(PluginInterface):
    def __init__(self):
        manifest = json.loads((_plugin_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest.setdefault("version", "0.0.0")  # Release packaging supplies the tag version.
        self._meta = PluginMeta(**manifest)
        self.dispatcher = None

    def get_meta(self):
        return self._meta

    async def on_load(self, context):
        global _active_dispatcher
        directory = context.get_fs().root
        state = await asyncio.to_thread(State, directory)
        dispatcher = Dispatcher(state, HostTasks(context))
        self.dispatcher = dispatcher
        _active_dispatcher = dispatcher
        context.hooks.register("after_upload", functools.partial(after_upload, str(directory)),
                               plugin_id=context.plugin_id)
        router = APIRouter()

        @router.get("/config")
        async def get_config():
            return {"config": public_config(await asyncio.to_thread(load_config, directory))}

        @router.get("/drives")
        async def get_drives():
            drives = await asyncio.to_thread(context.list_drives)
            return {"drives": [d for d in drives if d["drive_type"] == "115"]}

        @router.post("/config")
        async def save_config(body: dict):
            config = validate_config(body)
            async with dispatcher.lock:
                drives = await asyncio.to_thread(context.list_drives)
                try:
                    saved = await asyncio.to_thread(save_settings, state, config, drives, time.time())
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
            return {"ok": True, "config": saved}

        @router.get("/status")
        async def status(limit: int = 50, offset: int = 0):
            return await asyncio.to_thread(state.status, max(1, min(limit, 100)), max(0, offset))

        @router.post("/retry/{kind}/{record_id}")
        async def retry(kind: Literal["batch", "inbox"], record_id: int):
            def action():
                if kind == "batch":
                    state.retry(record_id, time.time())
                else:
                    with state.connect(write=True) as db:
                        config = state.current(db)
                        row = db.execute("SELECT * FROM inbox WHERE id=?", (record_id,)).fetchone()
                        if not row:
                            raise KeyError("记录不存在")
                        if row["target"] != target_key(config):
                            raise ValueError("请先恢复对应的网盘和 OneStrm 配置")
                        if row["task_id"] is None:
                            raise ValueError("该事件没有任务 ID，无法自动核查")
                        db.execute("UPDATE inbox SET error='' WHERE id=?", (record_id,))
            return await mutate(action)

        @router.post("/cancel/{kind}/{record_id}")
        async def cancel(kind: Literal["batch", "inbox"], record_id: int):
            return await mutate(functools.partial(state.cancel, kind, record_id, time.time()))

        async def mutate(action):
            try:
                await asyncio.to_thread(action)
            except KeyError:
                raise HTTPException(404, "记录不存在") from None
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return {"ok": True}

        @router.post("/history/clear")
        async def clear_history():
            await asyncio.to_thread(state.clear_history)
            return {"ok": True}

        @router.post("/test-connection")
        async def test_connection(body: dict):
            config = validate_config(body)
            if not config.base_url:
                raise HTTPException(422, "请填写 OneStrm 地址")
            return await probe_network(config.base_url)

        @router.post("/test-scan")
        async def test_scan(body: dict):
            try:
                path = cloud_path(body.get("path"))
                config = await asyncio.to_thread(load_config, directory)
                drives = await asyncio.to_thread(context.list_drives)
                if not any(d["id"] == config["drive_id"] and d["drive_type"] == "115" and d["is_active"] for d in drives):
                    raise ValueError("请先保存一个已启用的 115 网盘")
                return await dispatcher.test_scan(path)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

        context.register_router(router, prefix="/onestrm", tags=["OneStrm 自动工具"])
        context.register_job("dispatch", scheduled_poll, trigger_type="interval", trigger_args={"seconds": 2},
                             name="OneStrm 上传目录通知")

    async def on_unload(self):
        global _active_dispatcher
        if _active_dispatcher is self.dispatcher:
            _active_dispatcher = None
        if self.dispatcher is not None:
            await self.dispatcher.close()
