"""
通知系统 — 消息构建。

把宿主钩子的 `HookContext.data` 归一化成渠道无关的 `Message`（标题 + 字段列表），
再由各渠道各自渲染成自己的格式（Telegram HTML / 纯文本 / 未来的 Discord embed 等）。

钩子的 data 载荷字段在宿主各版本间可能有差异，这里用「候选键 + 优雅降级」的方式
提取，尽量做到无论 data 长什么样都能给出一条可读的通知。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Message:
    """渠道无关的通知消息。"""

    emoji: str
    title: str
    fields: List[Tuple[str, str]] = field(default_factory=list)
    level: str = "info"  # info / success / warning / error


# ── 提取 & 格式化辅助 ──


def _pick(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    """返回 data 中第一个存在且非空的键值。"""
    for key in keys:
        if key in data and data[key] not in (None, "", [], {}):
            return data[key]
    return None


def _short(text: str, limit: int = 100) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_size(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _with_time(fields: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    fields.append(("时间", _now_str()))
    return fields


# ── 各事件的格式化器 ──


def _format_upload(data: Dict[str, Any]) -> Message:
    file_obj = data.get("file") if isinstance(data.get("file"), dict) else {}
    name = _pick(data, "filename", "name", "local_path", "remote_path") or file_obj.get("name")
    remote = _pick(data, "remote_path", "target_path")
    drive = _pick(data, "drive_name", "drive", "drive_config_id")
    size = _pick(data, "file_size", "size") or file_obj.get("size")
    status = _pick(data, "status")
    error = _pick(data, "error_message", "error")

    failed = str(status).lower() in ("failed", "error") or bool(error)
    fields: List[Tuple[str, str]] = []
    if name:
        fields.append(("文件", _short(name)))
    if remote:
        fields.append(("目标", _short(remote)))
    if drive is not None:
        fields.append(("网盘", str(drive)))
    if size is not None:
        fields.append(("大小", _fmt_size(size)))
    if failed and error:
        fields.append(("错误", _short(str(error))))
    _with_time(fields)

    if failed:
        return Message("⚠️", "上传失败", fields, "error")
    return Message("✅", "上传完成", fields, "success")


def _format_sync(data: Dict[str, Any]) -> Message:
    rule = _pick(data, "rule_name", "name", "sync_rule_id")
    src = _pick(data, "source_path", "source")
    dst = _pick(data, "target_path", "target")
    done = _pick(data, "added", "uploaded", "transferred", "transferred_count", "count")
    failed = _pick(data, "failed", "failed_count")
    error = _pick(data, "error_message", "error")

    is_fail = bool(error) or (failed not in (None, 0, "0"))
    fields: List[Tuple[str, str]] = []
    if rule is not None:
        fields.append(("规则", str(rule)))
    if src and dst:
        fields.append(("路径", f"{_short(str(src), 48)} → {_short(str(dst), 48)}"))
    if done is not None:
        fields.append(("已同步", str(done)))
    if failed:
        fields.append(("失败", str(failed)))
    if error:
        fields.append(("错误", _short(str(error))))
    _with_time(fields)

    if is_fail:
        return Message("⚠️", "同步异常", fields, "warning")
    return Message("🔄", "同步完成", fields, "success")


def _format_error(data: Dict[str, Any]) -> Message:
    message = _pick(data, "message", "error", "error_message", "detail") or "未知错误"
    where = _pick(data, "where", "context", "source", "location", "operation")
    fields: List[Tuple[str, str]] = [("信息", _short(str(message)))]
    if where:
        fields.append(("来源", str(where)))
    _with_time(fields)
    return Message("❌", "发生错误", fields, "error")


def _format_startup(data: Dict[str, Any]) -> Message:
    version = _pick(data, "version", "app_version")
    fields: List[Tuple[str, str]] = []
    if version:
        fields.append(("版本", str(version)))
    _with_time(fields)
    return Message("🚀", "DriveCat 已启动", fields, "info")


def _format_generic(hook_name: str, data: Dict[str, Any]) -> Message:
    """未知事件的兜底格式化：挑几个标量字段展示。"""
    fields: List[Tuple[str, str]] = []
    for key, value in list(data.items())[:6]:
        if isinstance(value, (str, int, float, bool)):
            fields.append((str(key), _short(str(value))))
    _with_time(fields)
    return Message("🔔", hook_name, fields, "info")


_FORMATTERS = {
    "after_upload": _format_upload,
    "after_sync": _format_sync,
    "on_error": _format_error,
    "on_startup": _format_startup,
}


def format_event(hook_name: str, data: Optional[Dict[str, Any]]) -> Message:
    """把一个钩子事件转成 Message。"""
    data = data or {}
    formatter = _FORMATTERS.get(hook_name)
    if formatter is not None:
        return formatter(data)
    return _format_generic(hook_name, data)


def make_test_message(channel: str) -> Message:
    """测试按钮发送的示例消息。"""
    return Message(
        "🔔",
        "DriveCat 测试通知",
        _with_time([("渠道", channel), ("状态", "连接正常")]),
        "success",
    )
