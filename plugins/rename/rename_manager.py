"""
RenameManager — 重命名高层管理器。

协调 RenameRuleEngine + Drive 完成批量重命名。
支持并发执行与流式进度反馈。
"""

import asyncio
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel

from rename_engine import RenameRuleEngine, RuleSpec, create_rule


class RenamePreview(BaseModel):
    """单个文件的重命名预览。"""

    file_id: str
    original_name: str
    new_name: str
    changed: bool = False


class RenameResult(BaseModel):
    """批量重命名结果。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    details: List[Dict[str, Any]] = []


class RenameManager:
    """
    重命名管理器。

    使用方式：
        previews = await RenameManager.preview(drive, parent_id, rules)
        async for event in RenameManager.execute_stream(drive, parent_id, rules):
            print(event)
    """

    @staticmethod
    async def _build_plan(
        drive: Any,
        parent_id: str,
        rule_specs: List[RuleSpec],
        file_ids: Optional[List[str]] = None,
    ) -> List[tuple]:
        """构建 (idx, FileInfo, new_name) 列表。execute/execute_stream/preview 共用。"""
        rules = [create_rule(spec) for spec in rule_specs]
        all_files = await drive.list_files(parent_id)
        target_files = (
            [f for f in all_files if f.id in set(file_ids)]
            if file_ids
            else all_files
        )
        plan: List[tuple] = []
        for idx, f in enumerate(target_files):
            new_name = RenameRuleEngine.apply_rules(
                f.name, rules, index=idx,
                mtime=getattr(f, "modified_time", None) or getattr(f, "modified_at", None),
            )
            plan.append((idx, f, new_name))
        return plan

    @staticmethod
    async def preview(
        drive: Any,
        parent_id: str,
        rule_specs: List[RuleSpec],
        file_ids: Optional[List[str]] = None,
    ) -> List[RenamePreview]:
        """
        预览重命名结果（不执行）。

        Args:
            drive: Drive 实例
            parent_id: 目录 ID
            rule_specs: 规则列表
            file_ids: 限定文件 ID（None=目录下所有文件）
        """
        plan = await RenameManager._build_plan(drive, parent_id, rule_specs, file_ids)
        return [
            RenamePreview(
                file_id=f.id,
                original_name=f.name,
                new_name=new_name,
                changed=(new_name != f.name),
            )
            for _, f, new_name in plan
        ]

    @staticmethod
    async def execute(
        drive: Any,
        parent_id: str,
        rule_specs: List[RuleSpec],
        file_ids: Optional[List[str]] = None,
        concurrency: int = 10,
        pause_ms: int = 1000,
    ) -> RenameResult:
        """
        执行批量重命名（非流式，一次返回全部结果）。

        采用 worker 池流控：`concurrency` 个 worker 各自从队列取任务，
        每完成一个暂停 `pause_ms` 毫秒。单个慢请求只占住一个 worker，
        不会像批次屏障那样拖住整体。

        Args:
            drive: Drive 实例（需有 rename 方法，返回 bool）
            parent_id: 目录 ID
            rule_specs: 规则列表
            file_ids: 限定文件 ID
            concurrency: 并发 worker 数
            pause_ms: 每个任务完成后的暂停时长（毫秒）
        """
        if concurrency < 1:
            concurrency = 1
        plan = await RenameManager._build_plan(drive, parent_id, rule_specs, file_ids)
        result = RenameResult(total=len(plan))

        async def rename_one(f: Any, new_name: str) -> tuple:
            if new_name == f.name:
                return ("skipped", {"file_id": f.id, "name": f.name, "status": "skipped"})
            try:
                ok = await drive.rename(f.id, new_name)
            except Exception as exc:
                logger.warning(f"[Rename] Failed to rename {f.name}: {exc}")
                return ("failed", {
                    "file_id": f.id, "name": f.name,
                    "error": str(exc), "status": "failed",
                })
            if not ok:
                # 驱动吞了异常只返回 False（超时场景下实际可能已生效）
                logger.warning(f"[Rename] {f.name} → {new_name}: drive returned falsy")
                return ("failed", {
                    "file_id": f.id, "name": f.name,
                    "error": "驱动未确认成功（可能超时），请核对网盘实际状态",
                    "status": "failed",
                })
            logger.debug(f"[Rename] {f.name} → {new_name}")
            return ("success", {
                "file_id": f.id, "original": f.name,
                "new": new_name, "status": "success",
            })

        pause_sec = max(0, pause_ms) / 1000
        started = time.monotonic()

        work_q: asyncio.Queue = asyncio.Queue()
        for item in plan:
            work_q.put_nowait(item)

        async def worker():
            while True:
                try:
                    _, f, new_name = work_q.get_nowait()
                except asyncio.QueueEmpty:
                    return
                status, detail = await rename_one(f, new_name)
                if status == "success":
                    result.success += 1
                elif status == "failed":
                    result.failed += 1
                else:
                    result.skipped += 1
                result.details.append(detail)
                if pause_sec > 0:
                    await asyncio.sleep(pause_sec)

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, len(plan)))
        ]
        await asyncio.gather(*workers)

        elapsed = time.monotonic() - started
        rate = len(plan) / elapsed if elapsed > 0 else 0.0
        logger.info(
            f"[Rename] Done: {result.success} success, "
            f"{result.failed} failed, {result.skipped} skipped, "
            f"{elapsed:.1f}s ({rate:.1f}/s)"
        )
        return result

    @staticmethod
    async def execute_stream(
        drive: Any,
        parent_id: str,
        rule_specs: List[RuleSpec],
        file_ids: Optional[List[str]] = None,
        concurrency: int = 10,
        pause_ms: int = 1000,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式执行批量重命名，逐条 yield 事件给 SSE。

        采用 worker 池流控：`concurrency` 个 worker 各自从队列取任务，
        每完成一个暂停 `pause_ms` 毫秒；完成事件按实际完成顺序 yield。
        单个慢请求只占住一个 worker，不会像批次屏障那样拖住整体。

        事件格式：
          {"type": "start", "total": N}
          {"type": "progress", "index": i, "file_id": "...", "original": "...", "new": "...", "status": "success|skipped|failed"}
          {"type": "done", "total": N, "success": S, "failed": F, "skipped": K, "elapsed_ms": E, "rate": R}
        """
        if concurrency < 1:
            concurrency = 1
        plan = await RenameManager._build_plan(drive, parent_id, rule_specs, file_ids)
        total = len(plan)
        yield {"type": "start", "total": total}

        success = 0
        failed = 0
        skipped = 0

        async def rename_one(idx: int, f: Any, new_name: str) -> Dict[str, Any]:
            nonlocal success, failed, skipped
            if new_name == f.name:
                skipped += 1
                return {
                    "type": "progress", "index": idx,
                    "file_id": f.id, "original": f.name,
                    "new": new_name, "status": "skipped",
                }
            try:
                ok = await drive.rename(f.id, new_name)
            except Exception as exc:
                failed += 1
                logger.warning(f"[Rename] Failed: {f.name}: {exc}")
                return {
                    "type": "progress", "index": idx,
                    "file_id": f.id, "original": f.name,
                    "new": new_name, "status": "failed",
                    "error": str(exc),
                }
            if not ok:
                # 驱动吞了异常只返回 False（超时场景下实际可能已生效）
                failed += 1
                logger.warning(f"[Rename] {f.name} → {new_name}: drive returned falsy")
                return {
                    "type": "progress", "index": idx,
                    "file_id": f.id, "original": f.name,
                    "new": new_name, "status": "failed",
                    "error": "驱动未确认成功（可能超时），请核对网盘实际状态",
                }
            success += 1
            logger.debug(f"[Rename] {f.name} → {new_name}")
            return {
                "type": "progress", "index": idx,
                "file_id": f.id, "original": f.name,
                "new": new_name, "status": "success",
            }

        pause_sec = max(0, pause_ms) / 1000
        started = time.monotonic()

        work_q: asyncio.Queue = asyncio.Queue()
        event_q: asyncio.Queue = asyncio.Queue()
        for item in plan:
            work_q.put_nowait(item)

        async def worker():
            while True:
                try:
                    idx, f, new_name = work_q.get_nowait()
                except asyncio.QueueEmpty:
                    return
                # rename_one 内部已兜底所有预期异常，保证每个任务恰好产出一条事件
                await event_q.put(await rename_one(idx, f, new_name))
                if pause_sec > 0:
                    await asyncio.sleep(pause_sec)

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, total))
        ]
        remaining = total
        while remaining > 0:
            yield await event_q.get()
            remaining -= 1
        await asyncio.gather(*workers)

        elapsed = time.monotonic() - started
        rate = round(total / elapsed, 2) if elapsed > 0 else 0.0
        yield {
            "type": "done",
            "total": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "elapsed_ms": int(elapsed * 1000),
            "rate": rate,
        }

        logger.info(
            f"[Rename] Stream done: {success} success, "
            f"{failed} failed, {skipped} skipped, "
            f"{elapsed:.1f}s ({rate:.1f}/s)"
        )
