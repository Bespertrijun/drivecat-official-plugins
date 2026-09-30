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
#
# 宿主约定：after_* 钩子只代表成功，失败一律走 on_error（data["source"] 区分来源）。
# 同类事件的成功 / 失败共用一个格式化器，failed 决定标题与是否展示错误。


def _format_upload(data: Dict[str, Any], failed: bool = False) -> Message:
    """上传：after_upload（成功）/ on_error source=upload（失败）。"""
    name = _pick(data, "filename", "local_path", "remote_path")
    remote = _pick(data, "remote_path")
    drive = _pick(data, "drive_name", "drive_config_id")
    size = _pick(data, "file_size")
    error = _pick(data, "message", "error")

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


def _format_startup(data: Dict[str, Any]) -> Message:
    version = _pick(data, "version", "app_version")
    fields: List[Tuple[str, str]] = []
    if version:
        fields.append(("版本", str(version)))
    _with_time(fields)
    return Message("🚀", "DriveCat 已启动", fields, "info")


def _drive_pair(data: Dict[str, Any]) -> Optional[str]:
    src = _pick(data, "source_drive_name", "source_drive_id")
    dst = _pick(data, "target_drive_name", "target_drive_id")
    if src is None and dst is None:
        return None
    return f"{src if src is not None else '?'} → {dst if dst is not None else '?'}"


def _format_transfer(data: Dict[str, Any], failed: bool = False) -> Message:
    """单个文件转存：after_transfer（成功）/ on_error source=transfer（失败）。

    含文件夹批次子任务（带 rel_path）与同步产生的任务（带 sync_rule_id）。
    """
    name = _pick(data, "rel_path", "filename", "source_path")
    rule = _pick(data, "origin_label") if data.get("sync_rule_id") is not None else None
    src = _pick(data, "source_path")
    dst = _pick(data, "target_path")
    drives = _drive_pair(data)
    size = _pick(data, "file_size")
    error = _pick(data, "message", "error")

    fields: List[Tuple[str, str]] = []
    if rule:
        fields.append(("同步", _short(rule)))
    if name:
        fields.append(("文件", _short(name)))
    if src and dst:
        fields.append(("路径", f"{_short(str(src), 48)} → {_short(str(dst), 48)}"))
    if drives:
        fields.append(("网盘", drives))
    if size:
        fields.append(("大小", _fmt_size(size)))
    if failed and error:
        fields.append(("错误", _short(str(error))))
    _with_time(fields)

    if failed:
        return Message("⚠️", "转存失败", fields, "error")
    return Message("✅", "转存完成", fields, "success")


_BATCH_TITLES = {
    # (kind, failed) → (emoji, 标题, 名称字段标签)
    ("folder", False): ("📦", "文件夹转存完成", "名称"),
    ("folder", True): ("⚠️", "文件夹转存失败", "名称"),
    ("sync_run", False): ("🔄", "同步完成", "规则"),
    ("sync_run", True): ("⚠️", "同步异常", "规则"),
}


def _format_batch(data: Dict[str, Any], failed: bool = False, kind: Optional[str] = None) -> Message:
    """整批 / 整轮汇总：after_folder_transfer、after_sync（全部成功）/ on_error
    source=folder_transfer、sync_run（有文件失败，或同步中途出错 aborted）。

    kind 由调用方按钩子 / source 给出；缺省时看 data["kind"]。
    """
    kind = kind or ("sync_run" if data.get("kind") == "sync_run" else "folder")
    emoji, title, name_label = _BATCH_TITLES[(kind, failed)]
    name = _pick(data, "name")
    dst = _pick(data, "target_path")
    drives = _drive_pair(data)
    total = _pick(data, "total_files")
    success = data.get("success_count") or 0
    failed_count = data.get("failed_count") or 0
    skipped = (data.get("skipped_count") or 0) + (data.get("skipped_existing") or 0)
    size = _pick(data, "success_bytes")

    fields: List[Tuple[str, str]] = []
    if name:
        fields.append((name_label, _short(name)))
    if dst:
        fields.append(("目标", _short(dst)))
    if drives:
        fields.append(("网盘", drives))
    if total is not None:
        counts = f"成功 {success} · 失败 {failed_count} · 共 {total}"
        if skipped:
            counts += f"（另跳过 {skipped}）"
        fields.append(("文件", counts))
    if size:
        fields.append(("大小", _fmt_size(size)))
    if failed and data.get("aborted"):
        reason = _pick(data, "error")
        note = f"同步中途出错（{_short(str(reason), 80)}），" if reason else "同步中途出错，"
        fields.append(("说明", note + "仅统计已提交的文件"))
    _with_time(fields)

    if not failed:
        return Message(emoji, title, fields, "success")
    return Message(emoji, title, fields, "warning" if success else "error")


def _format_error(data: Dict[str, Any]) -> Message:
    """on_error：按 source 分派；未知来源给一条通用的"发生错误"。"""
    source = data.get("source")
    if source == "upload":
        return _format_upload(data, failed=True)
    if source == "transfer":
        return _format_transfer(data, failed=True)
    if source == "folder_transfer":
        return _format_batch(data, failed=True, kind="folder")
    if source == "sync_run":
        return _format_batch(data, failed=True, kind="sync_run")

    message = _pick(data, "message", "error", "error_message", "detail") or "未知错误"
    fields: List[Tuple[str, str]] = []
    if source == "drive_auth":
        drive = _pick(data, "drive_name") or f"网盘#{data.get('drive_id', '?')}"
        fields.append(("信息", _short(str(message), 200)))
        _with_time(fields)
        return Message("🔑", f"网盘凭证失效：{drive}", fields, "error")
    if source == "sync":
        rule = _pick(data, "rule_name", "sync_rule_id")
        if rule is not None:
            fields.append(("规则", _short(str(rule))))
        fields.append(("错误", _short(str(message), 200)))
        _with_time(fields)
        return Message("❌", "同步出错", fields, "error")

    fields.append(("信息", _short(str(message), 200)))
    if source:
        fields.append(("来源", str(source)))
    _with_time(fields)
    return Message("❌", "发生错误", fields, "error")


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
    "after_transfer": _format_transfer,
    "after_folder_transfer": lambda data: _format_batch(data, kind="folder"),
    "after_sync": lambda data: _format_batch(data, kind="sync_run"),
    "on_error": _format_error,
    "on_startup": _format_startup,
}

# 钩子 → 事件开关键（config.events）。成功按钩子各有开关；所有失败都走 on_error，归"发生错误"。
_HOOK_TOGGLES = {
    "after_upload": "after_upload",
    "after_folder_transfer": "transfer",
    "after_sync": "after_sync",
    "on_error": "on_error",
    "on_startup": "on_startup",
}


def event_toggles(hook_name: str, data: Optional[Dict[str, Any]]) -> List[str]:
    """事件需要的开关键（全部开启才推送）；返回空列表表示不推送。

    单个转存成功（after_transfer）按来源细分：
      - 同步产生（带 sync_rule_id）→ after_sync + sync_files；
      - 文件夹批次子任务（带 batch_id）→ transfer + transfer_batch_files；
      - 单文件转存 → transfer。
    """
    data = data or {}
    if hook_name == "after_transfer":
        if data.get("sync_rule_id") is not None:
            return ["after_sync", "sync_files"]
        if data.get("batch_id") is not None:
            return ["transfer", "transfer_batch_files"]
        return ["transfer"]
    toggle = _HOOK_TOGGLES.get(hook_name)
    return [toggle] if toggle else []


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
