"""
官方通知系统插件入口。

监听宿主事件并把通知推送到外部渠道（当前内置 Telegram）。

监听的钩子：
  after_upload  — 上传完成/失败
  after_sync    — 同步完成/异常
  on_error      — 发生错误
  on_startup    — 服务启动

API 端点：
  GET  /notify/config   — 读取当前配置
  POST /notify/config   — 保存配置（渠道 + 事件开关）
  POST /notify/test     — 发送一条测试通知（可用未保存的表单配置即时测试）

配置存储：plugin_data/{plugin_id}/config.json（通过 FileProxy）
"""

import json
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

# 让同目录模块可被导入（框架用 spec_from_file_location 加载，无包身份）
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from app.plugin.base import HookContext, PluginContext, PluginInterface, PluginMeta
from notify_notifier import Notifier

# 需注册的钩子（与 manifest.hooks 保持一致）
HOOKS = ["after_upload", "after_sync", "on_error", "on_startup"]


# ── API 请求模型 ──


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    parse_mode: str = "HTML"


class ChannelsConfig(BaseModel):
    telegram: TelegramConfig = TelegramConfig()


class EventsConfig(BaseModel):
    after_upload: bool = True
    after_sync: bool = True
    on_error: bool = True
    on_startup: bool = False


class ConfigBody(BaseModel):
    channels: ChannelsConfig = ChannelsConfig()
    events: EventsConfig = EventsConfig()


class TestBody(BaseModel):
    channel: str = "telegram"
    telegram: Optional[TelegramConfig] = None


# ── 插件主体 ──


class NotifyPlugin(PluginInterface):
    """官方通知系统插件。"""

    def __init__(self):
        self._context: Optional[PluginContext] = None
        self._notifier: Optional[Notifier] = None
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
            self._meta = PluginMeta(**json.load(f))

    def get_meta(self) -> PluginMeta:
        return self._meta

    async def on_load(self, context: PluginContext) -> None:
        self._context = context
        self._notifier = Notifier(context.get_fs(), context.logger)

        # ── 注册钩子 ──
        for hook_name in HOOKS:
            context.hooks.register(
                hook_name,
                self._make_handler(hook_name),
                plugin_id=context.plugin_id,
            )

        # ── 注册路由 ──
        router = APIRouter()

        @router.get("/config")
        async def get_config():
            return {"config": self._notifier.get_config()}

        @router.post("/config")
        async def save_config(body: ConfigBody):
            cfg = self._notifier.save_config(body.model_dump())
            return {"ok": True, "config": cfg}

        @router.post("/test")
        async def test(body: TestBody):
            ok, error = await self._notifier.send_test(
                body.model_dump(exclude_none=True)
            )
            return {"ok": ok, "error": error}

        context.register_router(router, prefix="/notify", tags=["通知系统"])
        context.logger.info("[NotifyPlugin] Loaded")

    async def on_unload(self) -> None:
        if self._context:
            self._context.logger.info("[NotifyPlugin] Unloaded")
        self._context = None
        self._notifier = None

    # ── 钩子处理 ──

    def _make_handler(self, hook_name: str):
        """为某个钩子生成 handler：把事件交给 Notifier 分发，永不修改数据。"""

        async def handler(ctx: HookContext) -> Optional[HookContext]:
            await self._notifier.dispatch(hook_name, ctx.data)
            return None

        return handler
