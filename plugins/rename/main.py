"""
官方重命名插件入口。

API 端点：
  POST /rename/preview      — 预览重命名结果
  POST /rename/execute      — 执行重命名（SSE 流式进度）
  GET  /rename/templates    — 获取已存模板列表
  POST /rename/templates    — 保存模板
  DELETE /rename/templates/{name} — 删除模板

模板存储：plugin_data/{plugin_id}/templates.json（通过 FileProxy）
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


class ExecuteRequest(RenameRequest):
    """执行重命名请求体，批次流控。"""

    concurrency: int = Field(default=10, ge=1, le=50)
    pause_ms: int = Field(default=1000, ge=0, le=60000)


class TemplateData(BaseModel):
    """模板保存请求。"""

    name: str
    rules: List[RuleSpec]


# ── 插件主体 ──


class RenamePlugin(PluginInterface):
    """官方重命名插件。"""

    def __init__(self):
        self._context: Optional[PluginContext] = None
        # 从 manifest.json 读取元数据，消除双份真相源
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
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

        # --- 预览 ---
        @router.post("/preview")
        async def preview(req: RenameRequest):
            drive = await context.get_drive(req.drive_config_id)
            previews = await RenameManager.preview(
                drive, req.parent_id, req.rules, req.file_ids,
            )
            return {"previews": [p.model_dump() for p in previews]}

        # --- 执行（SSE 流式进度） ---
        @router.post("/execute")
        async def execute(req: ExecuteRequest):
            drive = await context.get_drive(req.drive_config_id)

            async def sse_stream():
                async for event in RenameManager.execute_stream(
                    drive, req.parent_id, req.rules,
                    file_ids=req.file_ids,
                    concurrency=req.concurrency,
                    pause_ms=req.pause_ms,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # --- 模板 CRUD ---
        TEMPLATES_FILE = "templates.json"

        @router.get("/templates")
        async def list_templates():
            """列出所有模板。"""
            fs = context.get_fs()
            if not fs.exists(TEMPLATES_FILE):
                return {"templates": []}
            data = json.loads(fs.read_text(TEMPLATES_FILE))
            return {"templates": data}

        @router.post("/templates")
        async def save_template(tpl: TemplateData):
            """保存一个模板（同名覆盖）。"""
            fs = context.get_fs()
            templates: List[Dict[str, Any]] = []
            if fs.exists(TEMPLATES_FILE):
                templates = json.loads(fs.read_text(TEMPLATES_FILE))
            # 同名覆盖
            templates = [t for t in templates if t["name"] != tpl.name]
            templates.append({
                "name": tpl.name,
                "rules": [r.model_dump() for r in tpl.rules],
            })
            fs.write_text(
                TEMPLATES_FILE,
                json.dumps(templates, ensure_ascii=False, indent=2),
            )
            return {"ok": True}

        @router.delete("/templates/{name}")
        async def delete_template(name: str):
            """删除一个模板。"""
            fs = context.get_fs()
            if not fs.exists(TEMPLATES_FILE):
                return {"ok": True}
            templates = json.loads(fs.read_text(TEMPLATES_FILE))
            templates = [t for t in templates if t["name"] != name]
            fs.write_text(
                TEMPLATES_FILE,
                json.dumps(templates, ensure_ascii=False, indent=2),
            )
            return {"ok": True}

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
