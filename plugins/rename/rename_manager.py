"""
RenameManager — 重命名高层管理器。

协调 RenameRuleEngine + Drive 完成批量重命名。
"""

import os
from typing import Any, Dict, List, Optional

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
        mgr = RenameManager()
        previews = await mgr.preview(drive, parent_id, rules)
        result = await mgr.execute(drive, parent_id, rules)
    """

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
        rules = [create_rule(spec) for spec in rule_specs]
        all_files = await drive.list_files(parent_id)

        # 先过滤，再枚举——确保 index 和 total 基于目标集合
        target_files = (
            [f for f in all_files if f.id in set(file_ids)]
            if file_ids
            else all_files
        )

        previews = []
        for idx, f in enumerate(target_files):
            new_name = RenameRuleEngine.apply_rules(
                f.name, rules, index=idx,
                mtime=getattr(f, "modified_time", None) or getattr(f, "modified_at", None),
            )

            previews.append(
                RenamePreview(
                    file_id=f.id,
                    original_name=f.name,
                    new_name=new_name,
                    changed=(new_name != f.name),
                )
            )

        return previews

    @staticmethod
    async def execute(
        drive: Any,
        parent_id: str,
        rule_specs: List[RuleSpec],
        file_ids: Optional[List[str]] = None,
    ) -> RenameResult:
        """
        执行批量重命名。

        Args:
            drive: Drive 实例（需有 rename 方法）
            parent_id: 目录 ID
            rule_specs: 规则列表
            file_ids: 限定文件 ID
        """
        rules = [create_rule(spec) for spec in rule_specs]
        all_files = await drive.list_files(parent_id)

        # 先过滤，再枚举——确保 index 和 total 基于目标集合
        target_files = (
            [f for f in all_files if f.id in set(file_ids)]
            if file_ids
            else all_files
        )

        result = RenameResult(total=len(target_files))

        for idx, f in enumerate(target_files):
            new_name = RenameRuleEngine.apply_rules(
                f.name, rules, index=idx,
                mtime=getattr(f, "modified_time", None) or getattr(f, "modified_at", None),
            )

            if new_name == f.name:
                result.skipped += 1
                result.details.append(
                    {"file_id": f.id, "name": f.name, "status": "skipped"}
                )
                continue

            try:
                await drive.rename(f.id, new_name)
                result.success += 1
                result.details.append(
                    {
                        "file_id": f.id,
                        "original": f.name,
                        "new": new_name,
                        "status": "success",
                    }
                )
                logger.debug(f"[Rename] {f.name} → {new_name}")

            except Exception as exc:
                result.failed += 1
                result.details.append(
                    {
                        "file_id": f.id,
                        "name": f.name,
                        "error": str(exc),
                        "status": "failed",
                    }
                )
                logger.warning(f"[Rename] Failed to rename {f.name}: {exc}")

        logger.info(
            f"[Rename] Done: {result.success} success, "
            f"{result.failed} failed, {result.skipped} skipped"
        )
        return result

