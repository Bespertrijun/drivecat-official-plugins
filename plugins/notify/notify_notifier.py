"""
通知系统 — 调度核心。

Notifier 持有当前配置，负责：
    - 收到钩子事件 → 判断该事件是否开启 → 构建消息 → 分发到所有已启用渠道；
    - 提供测试发送（可用未落盘的表单配置即时测试）。

分发采用「后台任务 + 全捕获」策略：不阻塞宿主上传/同步流程，
且通知失败绝不向上抛（否则钩子连续失败会被宿主自动禁用）。
"""

import asyncio
from typing import Any, Dict, Optional, Tuple

from notify_channels import build_channel, build_enabled_channels
from notify_config import load_config, save_config
from notify_messages import format_event, make_test_message


class Notifier:

    def __init__(self, fs, logger):
        self._fs = fs
        self._log = logger
        self._config: Dict[str, Any] = load_config(fs)

    # ── 配置 ──

    def get_config(self) -> Dict[str, Any]:
        return self._config

    def reload(self) -> Dict[str, Any]:
        self._config = load_config(self._fs)
        return self._config

    def save_config(self, new_config: Dict[str, Any]) -> Dict[str, Any]:
        self._config = save_config(self._fs, new_config)
        return self._config

    # ── 事件分发 ──

    async def dispatch(self, hook_name: str, data: Optional[Dict[str, Any]]) -> None:
        try:
            if not (self._config.get("events") or {}).get(hook_name, False):
                return
            channels = build_enabled_channels(self._config)
            if not channels:
                return
            message = format_event(hook_name, data)
            for channel in channels:
                asyncio.create_task(self._safe_send(channel, message))
        except Exception as exc:  # 分发本身不应影响宿主流程
            self._warn(f"dispatch({hook_name}) 出错：{exc}")

    async def _safe_send(self, channel, message) -> None:
        try:
            await channel.send(message)
        except Exception as exc:
            self._warn(f"经 {channel.name} 发送失败：{exc}")

    # ── 测试发送 ──

    async def send_test(self, body: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        """发送测试消息。body 可携带未落盘的渠道配置以便先测后存。"""
        body = body or {}
        channel_name = body.get("channel") or "telegram"
        # 优先用请求体里的即时配置，否则回落到已保存配置
        cfg = body.get(channel_name) or (self._config.get("channels") or {}).get(channel_name)
        if not cfg:
            return False, f"未找到渠道配置：{channel_name}"

        channel = build_channel(channel_name, cfg)
        if channel is None:
            return False, "配置不完整（请填写 Bot Token 与 Chat ID）"

        try:
            await channel.send(make_test_message(channel_name))
            return True, None
        except Exception as exc:
            return False, str(exc)

    # ── 内部 ──

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(f"[notify] {msg}")
