"""
RenameManager — 重命名高层管理器。

协调 RenameRuleEngine + Drive 完成批量重命名。
支持并发执行与流式进度反馈。
"""

import asyncio
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

        采用批次流控：每批最多 `concurrency` 个文件并发，
        批与批之间暂停 `pause_ms` 毫秒避免限流。

        Args:
            drive: Drive 实例（需有 rename 方法）
            parent_id: 目录 ID
            rule_specs: 规则列表
            file_ids: 限定文件 ID
            concurrency: 每批并发数
            pause_ms: 批与批之间的暂停时长（毫秒）
        """
        if concurrency < 1:
            concurrency = 1
        plan = await RenameManager._build_plan(drive, parent_id, rule_specs, file_ids)
        result = RenameResult(total=len(plan))

        async def rename_one(f: Any, new_name: str) -> tuple:
            if new_name == f.name:
                return ("skipped", {"file_id": f.id, "name": f.name, "status": "skipped"})
            try:
                await drive.rename(f.id, new_name)
                logger.debug(f"[Rename] {f.name} → {new_name}")
                return ("success", {
                    "file_id": f.id, "original": f.name,
                    "new": new_name, "status": "success",
                })
            except Exception as exc:
                logger.warning(f"[Rename] Failed to rename {f.name}: {exc}")
                return ("failed", {
                    "file_id": f.id, "name": f.name,
                    "error": str(exc), "status": "failed",
                })

        pause_sec = max(0, pause_ms) / 1000
        for batch_start in range(0, len(plan), concurrency):
            batch = plan[batch_start : batch_start + concurrency]
            outcomes = await asyncio.gather(*[rename_one(f, n) for _, f, n in batch])
            for status, detail in outcomes:
                if status == "success":
                    result.success += 1
                elif status == "failed":
                    result.failed += 1
                else:
                    result.skipped += 1
                result.details.append(detail)

            # 批间暂停（最后一批不暂停）
            if pause_sec > 0 and batch_start + concurrency < len(plan):
                await asyncio.sleep(pause_sec)

        logger.info(
            f"[Rename] Done: {result.success} success, "
            f"{result.failed} failed, {result.skipped} skipped"
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

        采用批次流控：每批最多 `concurrency` 个文件并发，
        批内逐个 yield 完成事件；批与批之间暂停 `pause_ms` 毫秒。

        事件格式：
          {"type": "start", "total": N}
          {"type": "progress", "index": i, "file_id": "...", "original": "...", "new": "...", "status": "success|skipped|failed"}
          {"type": "done", "total": N, "success": S, "failed": F, "skipped": K}
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
                await drive.rename(f.id, new_name)
                success += 1
                logger.debug(f"[Rename] {f.name} → {new_name}")
                return {
                    "type": "progress", "index": idx,
                    "file_id": f.id, "original": f.name,
                    "new": new_name, "status": "success",
                }
            except Exception as exc:
                failed += 1
                logger.warning(f"[Rename] Failed: {f.name}: {exc}")
                return {
                    "type": "progress", "index": idx,
                    "file_id": f.id, "original": f.name,
                    "new": new_name, "status": "failed",
                    "error": str(exc),
                }

        pause_sec = max(0, pause_ms) / 1000
        for batch_start in range(0, total, concurrency):
            batch = plan[batch_start : batch_start + concurrency]
            # 批内并发，as_completed 让先完成的先 yield
            coros = [rename_one(idx, f, n) for idx, f, n in batch]
            for coro in asyncio.as_completed(coros):
                event = await coro
                yield event

            # 批间暂停（最后一批不暂停）
            if pause_sec > 0 and batch_start + concurrency < total:
                await asyncio.sleep(pause_sec)

        yield {
            "type": "done",
            "total": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
        }

        logger.info(
            f"[Rename] Stream done: {success} success, "
            f"{failed} failed, {skipped} skipped"
        )
