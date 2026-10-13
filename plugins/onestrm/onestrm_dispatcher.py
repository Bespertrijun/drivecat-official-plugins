"""Read host upload records, then dispatch the plugin's durable directory queue."""

import asyncio
import time
from datetime import datetime, timezone

from onestrm_client import notify
from onestrm_config import cloud_path, target_key


def timestamp(value):
    if not isinstance(value, datetime):
        raise TypeError("缺少有效的上传完成时间")
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()


class HostTasks:
    """Only uses the existing DbProxy allowlist and read permission."""

    def __init__(self, context):
        self.context = context

    @staticmethod
    def record(row):
        return {name: getattr(row, name, None) for name in
                ("id", "drive_config_id", "remote_path", "status", "completed_at")}

    def by_ids(self, ids):
        from app.models.watch import UploadTask
        db = self.context.get_db()
        try:
            return {r.id: self.record(r) for r in db.query(UploadTask).filter(UploadTask.id.in_(ids)).all()}
        finally:
            db.close()

    def page(self, drive_id, interval):
        from app.models.watch import UploadTask
        from sqlalchemy import and_, or_
        db = self.context.get_db()
        try:
            cursor = datetime.fromtimestamp(interval["cursor_time"], timezone.utc).replace(tzinfo=None)
            upper = datetime.fromtimestamp(interval["round_end"], timezone.utc).replace(tzinfo=None)
            query = db.query(UploadTask).filter(
                UploadTask.drive_config_id == drive_id, UploadTask.status == "success",
                UploadTask.completed_at < upper,
                or_(UploadTask.completed_at > cursor,
                    and_(UploadTask.completed_at == cursor, UploadTask.id > interval["cursor_id"])))
            return [self.record(r) for r in query.order_by(UploadTask.completed_at, UploadTask.id).limit(500).all()]
        finally:
            db.close()


def checked_task(raw, drive_id, now):
    if raw is None:
        return None, "宿主上传记录缺失，待核查"
    if raw["status"] != "success" or raw["drive_config_id"] != drive_id:
        return None, ""  # Ignore failures, skipped tasks and other accounts.
    task = dict(raw)
    try:
        task["completed_at"] = timestamp(raw["completed_at"])
        if task["completed_at"] > now:
            return task, "上传完成时间晚于当前时间，待核查"
        task["path"] = cloud_path(raw["remote_path"], file=True)
    except (ValueError, TypeError) as exc:
        return task, str(exc)
    return task, ""


class Dispatcher:
    def __init__(self, state, host, sender=notify, clock=time.time):
        self.state, self.host, self.sender, self.clock = state, host, sender, clock
        self.lock = asyncio.Lock()
        self.stopped = False

    def prepare(self):
        now = self.clock()
        self.state.maintain(now)
        with self.state.connect(write=True) as db:
            config = self.state.current(db)
        if not config["enabled"]:
            return
        self.state.diagnostic("")
        key = target_key(config)
        clues = self.state.clues(key)
        if clues:
            tasks = self.host.by_ids([c["task_id"] for c in clues])
            results = []
            for clue in clues:
                task, error = checked_task(tasks.get(clue["task_id"]), config["drive_id"], now)
                if task and not error and clue["remote_path"] != task["remote_path"]:
                    error = "事件与宿主记录的远程路径不一致，待核查"
                results.append((clue, task, error))
            self.state.consume_clues(key, results, now, config["quiet_seconds"])
        # Bounded work per tick; cursor and inserted tasks commit together.
        for _ in range(2):
            interval = self.state.begin_page(key, now)
            if not interval:
                break
            rows = self.host.page(config["drive_id"], interval)
            tasks = [checked_task(r, config["drive_id"], now) for r in rows]
            cursor = (timestamp(rows[-1]["completed_at"]), rows[-1]["id"]) if rows else None
            self.state.commit_page(interval, tasks, len(rows), cursor, now, config["quiet_seconds"])

    async def deliver(self, manual_id=None):
        if self.stopped:
            return None
        claimed = await asyncio.to_thread(self.state.claim, self.clock(), manual_id)
        if claimed is None:
            return None
        # Cancellation leaves the lease durable; a restart can recover an uncertain request.
        result = await self.sender(claimed["config"], claimed["path"])
        await asyncio.to_thread(self.state.finish, claimed, result, self.clock())
        return result

    async def run(self):
        if self.stopped or self.lock.locked():
            return
        async with self.lock:
            if self.stopped:
                return
            try:
                await asyncio.to_thread(self.prepare)
            except Exception:  # noqa: BLE001 - report a sanitized diagnostic at the host boundary
                # Never expose raw DB errors, URLs, credentials or host row contents.
                await asyncio.to_thread(self.state.diagnostic, "上传记录补查失败，请检查宿主兼容性或数据库状态")
            # Already persisted work remains deliverable during a temporary host DB outage.
            try:
                await self.deliver()
            except Exception:  # noqa: BLE001 - preserve the lease on unexpected processing failure
                await asyncio.to_thread(self.state.diagnostic, "通知处理失败；已保存的任务将在租约到期后恢复")

    async def close(self):
        self.stopped = True
        async with self.lock:
            pass  # Wait for the bounded request already in progress; claim no new work.

    async def test_scan(self, path):
        async with self.lock:
            if self.stopped:
                raise ValueError("插件正在停止")

            def enqueue():
                with self.state.connect(write=True) as db:
                    config = self.state.current(db)
                    if not target_key(config):
                        raise ValueError("请先保存网盘和 OneStrm 地址")
                    return self.state.enqueue(db, target_key(config), None, path, self.clock(), 0)

            batch_id = await asyncio.to_thread(enqueue)
            result = await self.deliver(batch_id)
            return {"ok": bool(result and result.ok), "queued": result is None,
                    "message": "已加入待发送队列" if result is None else
                    "通知成功（HTTP 已接收）；STRM 生成请在 OneStrm 确认" if result.ok else result.error}
