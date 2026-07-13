"""
通知系统 — 渠道抽象基类。

新增一个渠道（如 Discord / Slack）只需：
    1. 新建 notify_channel_xxx.py，实现 NotifyChannel 子类；
    2. 在 notify_channels.REGISTRY 里注册 `{ClassName.name: ClassName}`；
    3. 在 notify_config.DEFAULT_CONFIG 的 channels 下加一段默认配置。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from notify_messages import Message


class NotifyChannel(ABC):
    """一个通知渠道。无状态地把 Message 发送出去。"""

    # 渠道标识，同时作为配置 key 与 REGISTRY key
    name: str = "base"

    @classmethod
    @abstractmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "Optional[NotifyChannel]":
        """从渠道配置构造实例；配置不完整时返回 None。"""
        ...

    @abstractmethod
    async def send(self, message: Message) -> None:
        """发送一条消息。失败时抛出异常（由上层捕获并记录）。"""
        ...
