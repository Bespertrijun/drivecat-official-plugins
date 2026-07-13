"""
通知系统 — Telegram 渠道。

通过 Telegram Bot API 的 sendMessage 接口推送。用标准库 urllib 发请求，
放进线程池执行（asyncio.to_thread），避免阻塞宿主事件循环，也不引入额外依赖。

获取 bot_token：在 Telegram 里找 @BotFather 创建机器人。
获取 chat_id：给机器人发一条消息后访问
    https://api.telegram.org/bot<token>/getUpdates
    读取 result[].message.chat.id（群组为负数）。
"""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from notify_channel_base import NotifyChannel
from notify_messages import Message

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 15


def _esc(text: Any) -> str:
    """转义 Telegram HTML 特殊字符。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TelegramChannel(NotifyChannel):

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, parse_mode: str = "HTML"):
        self._token = bot_token
        self._chat_id = chat_id
        self._parse_mode = (parse_mode or "").strip()

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "Optional[TelegramChannel]":
        token = (cfg.get("bot_token") or "").strip()
        chat_id = str(cfg.get("chat_id") or "").strip()
        if not token or not chat_id:
            return None
        return cls(token, chat_id, cfg.get("parse_mode") or "HTML")

    # ── 渲染 ──

    def render(self, message: Message) -> str:
        if self._parse_mode.lower() == "html":
            lines = [f"<b>{_esc(message.emoji)} {_esc(message.title)}</b>"]
            for label, value in message.fields:
                lines.append(f"{_esc(label)}：<code>{_esc(value)}</code>")
            return "\n".join(lines)
        # 纯文本（parse_mode 为空或不支持时）
        lines = [f"{message.emoji} {message.title}"]
        for label, value in message.fields:
            lines.append(f"{label}：{value}")
        return "\n".join(lines)

    # ── 发送 ──

    async def send(self, message: Message) -> None:
        await self._post(self.render(message))

    async def _post(self, text: str) -> None:
        url = _API.format(token=self._token)
        payload: Dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if self._parse_mode:
            payload["parse_mode"] = self._parse_mode
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _do() -> None:
            try:
                with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                    resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                try:
                    detail = json.loads(detail).get("description", detail)
                except Exception:
                    pass
                raise RuntimeError(f"Telegram 返回 {exc.code}：{detail}")
            except urllib.error.URLError as exc:
                raise RuntimeError(f"网络错误：{exc.reason}")

        await asyncio.to_thread(_do)
