"""
通知系统 — 渠道注册表与工厂。

REGISTRY 是「渠道名 → 渠道类」的唯一登记处。新增渠道只需在此登记。
"""

from typing import Any, Dict, List, Optional

from notify_channel_base import NotifyChannel
from notify_channel_telegram import TelegramChannel

REGISTRY: Dict[str, type] = {
    TelegramChannel.name: TelegramChannel,
    # 后续在此登记：DiscordChannel.name: DiscordChannel, ...
}


def build_channel(name: str, cfg: Dict[str, Any]) -> Optional[NotifyChannel]:
    """按渠道名构造单个渠道实例；未知渠道或配置不完整时返回 None。"""
    cls = REGISTRY.get(name)
    if cls is None:
        return None
    return cls.from_config(cfg or {})


def build_enabled_channels(config: Dict[str, Any]) -> List[NotifyChannel]:
    """构造所有「已启用且配置完整」的渠道。"""
    out: List[NotifyChannel] = []
    for name, cfg in (config.get("channels") or {}).items():
        if not (cfg or {}).get("enabled"):
            continue
        channel = build_channel(name, cfg)
        if channel is not None:
            out.append(channel)
    return out
