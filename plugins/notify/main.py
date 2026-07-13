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

import asyncio
import functools
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

# 兄弟模块（notify_channels / notify_config / notify_messages / notify_notifier）一律
# 延迟到「真正用到的位置」再导入，不在模块顶层导入。原因：这些文件由市场打包下发，
# 万一某次打包/发布异常导致其中一个文件缺失或损坏，顶层导入会整体失败，令插件彻底
# 加载不起来（路由 / UI / 测试按钮全灭，正如 v1.0.2 装到旧包时那样）。延迟导入后，
# 坏文件只会让「用到它的那条路径」失效，其余功能照常。

# 需注册的钩子（与 manifest.hooks 保持一致）
HOOKS = ["after_upload", "after_sync", "on_error", "on_startup"]


# ── 沙箱子进程钩子入口 ──
#
# 宿主 PluginSandbox 用 ProcessPoolExecutor 在【独立子进程】里同步执行 handler
# （loop.run_in_executor(pool, handler, ctx)）。因此钩子 handler 必须：
#   ① 是同步函数——协程会被 run_in_executor 直接拒绝（“coroutines cannot be
#      used with run_in_executor()”，正是之前上传成功却无通知的根因）；
#   ② 可被 pickle——只能是模块级函数（配合 functools.partial 传参），
#      实例方法 / 闭包都无法被 pickle 到子进程；
#   ③ 自包含——子进程拿不到插件实例、FileProxy 与事件循环，故配置按绝对路径
#      现读，发送时用 asyncio.run 自起临时事件循环。


async def _send_all(channels, message) -> None:
    """依次向所有渠道发送；单个渠道失败不影响其它渠道。"""
    for channel in channels:
        try:
            await channel.send(message)
        except Exception:
            pass


def _dispatch_event(config_dir: Optional[str], hook_name: str, ctx: HookContext) -> None:
    """在沙箱子进程中处理一个钩子事件：判断开关 → 构建消息 → 同步发送。

    任何异常都被吞掉：通知失败绝不能反馈给沙箱（连续 3 次失败会触发插件自动
    禁用），更不能影响宿主的上传/同步主流程。
    """
    try:
        if not config_dir:
            return
        # 延迟导入（见文件顶部说明）：置于 try 内，兄弟模块缺失/损坏也只是安静跳过本次通知。
        from notify_channels import build_enabled_channels
        from notify_config import load_config_from_dir
        from notify_messages import format_event

        config = load_config_from_dir(config_dir)
        if not (config.get("events") or {}).get(hook_name, False):
            return
        channels = build_enabled_channels(config)
        if not channels:
            return
        message = format_event(hook_name, ctx.data if ctx else {})
        asyncio.run(_send_all(channels, message))
    except Exception:
        pass


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
        self._notifier = None  # 延迟构建的 Notifier（见 _get_notifier）
        self._config_dir: Optional[str] = None
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
            self._meta = PluginMeta(**json.load(f))

    def get_meta(self) -> PluginMeta:
        return self._meta

    async def on_load(self, context: PluginContext) -> None:
        self._context = context

        # ── 解析配置目录 ──
        # 钩子在沙箱【子进程】里执行，拿不到 FileProxy，只能按绝对路径现读 config.json，
        # 故在此把目录解析出来、稍后 pickle 给子进程。整段包在 try 里：即便解析失败也
        # 绝不能中断 on_load（否则下面的 register_router 被跳过 → 所有 /notify/* 变 404）；
        # 最坏情况仅是钩子通知不可用，API 路由与测试按钮照常工作。
        try:
            self._config_dir = context.get_fs().root
        except Exception as exc:
            self._config_dir = None
            context.logger.warning(
                f"[NotifyPlugin] 配置目录解析失败，钩子通知暂不可用：{exc}"
            )

        # ── 注册钩子 ──
        # handler 必须是「同步 + 可 pickle + 自包含」的模块级函数，详见 _dispatch_event 注释。
        for hook_name in HOOKS:
            context.hooks.register(
                hook_name,
                functools.partial(_dispatch_event, self._config_dir, hook_name),
                plugin_id=context.plugin_id,
            )

        # ── 注册路由 ──
        router = APIRouter()

        @router.get("/config")
        async def get_config():
            return {"config": self._get_notifier().get_config()}

        @router.post("/config")
        async def save_config(body: ConfigBody):
            cfg = self._get_notifier().save_config(body.model_dump())
            return {"ok": True, "config": cfg}

        @router.post("/test")
        async def test(body: TestBody):
            ok, error = await self._get_notifier().send_test(
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

    # ── 惰性构建 ──

    def _get_notifier(self) -> "Notifier":
        """首次使用时才解析 fs 并构建 Notifier（避免加载期触碰宿主资源）。

        Notifier 亦在此处延迟导入：它会连带导入 notify_config 等兄弟模块，放到这里可
        避免顶层导入失败拖垮整个插件（见文件顶部说明）。
        """
        if self._notifier is None:
            from notify_notifier import Notifier

            self._notifier = Notifier(self._context.get_fs(), self._context.logger)
        return self._notifier
