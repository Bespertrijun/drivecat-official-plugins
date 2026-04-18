"""
官方重命名插件入口。
"""

import sys
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

# 让插件的其他模块可以被导入
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from app.plugin.base import HookContext, PluginContext, PluginInterface, PluginMeta
from rename_engine import RuleSpec
from rename_manager import RenameManager


# ── API 请求/响应模型 ──


class RenameRequest(BaseModel):
    """重命名 API 请求体。"""

    drive_config_id: int
    parent_id: str
    rules: List[RuleSpec]
    file_ids: Optional[List[str]] = None


# ── 插件主体 ──


class RenamePlugin(PluginInterface):
    """官方重命名插件。"""

    def __init__(self):
        self._context: Optional[PluginContext] = None
        # 从 manifest.json 读取元数据，消除双份真相源
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
            import json
            data = json.load(f)
        self._meta = PluginMeta(**data)

    def get_meta(self) -> PluginMeta:
        return self._meta

    async def on_load(self, context: PluginContext) -> None:
        self._context = context

        # ── 注册钩子 ──

        context.hooks.register(
            "before_rename",
            self._on_before_rename,
            plugin_id=context.plugin_id,
        )
        context.hooks.register(
            "after_rename",
            self._on_after_rename,
            plugin_id=context.plugin_id,
        )

        # ── 注册路由 ──

        router = APIRouter()

        @router.post("/preview")
        async def preview(req: RenameRequest):
            drive = await context.get_drive(req.drive_config_id)
            previews = await RenameManager.preview(
                drive, req.parent_id, req.rules, req.file_ids,
            )
            return {"previews": [p.model_dump() for p in previews]}

        @router.post("/execute")
        async def execute(req: RenameRequest):
            drive = await context.get_drive(req.drive_config_id)
            result = await RenameManager.execute(
                drive, req.parent_id, req.rules, req.file_ids,
            )
            return result.model_dump()

        context.register_router(router, prefix="/rename", tags=["重命名"])

        context.logger.info("[RenamePlugin] Loaded")

    async def on_unload(self) -> None:
        if self._context:
            self._context.logger.info("[RenamePlugin] Unloaded")
        self._context = None

    # ── 钩子处理函数 ──

    @staticmethod
    async def _on_before_rename(ctx: HookContext) -> Optional[HookContext]:
        """重命名前：可在此做校验/预处理。"""
        return ctx

    @staticmethod
    async def _on_after_rename(ctx: HookContext) -> Optional[HookContext]:
        """重命名后：可在此做日志/通知。"""
        return ctx
