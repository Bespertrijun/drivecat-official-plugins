"""
官方重命名插件入口。
"""

import sys
from pathlib import Path

# 让插件的其他模块可以被导入
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from app.plugin.base import HookContext, PluginContext, PluginInterface, PluginMeta
from rename_manager import RenameManager


class RenamePlugin(PluginInterface):
    """官方重命名插件。"""

    def __init__(self):
        self._context = None

    def get_meta(self) -> PluginMeta:
        return PluginMeta(
            name="网盘文件批量重命名",
            version="1.0.0",
            author="DriveCat",
            description="支持 8 种规则的网盘文件批量重命名",
            hooks=["before_rename", "after_rename"],
            permissions=["drive.list", "drive.rename"],
            source="official",
            entry="main.RenamePlugin",
        )

    async def on_load(self, context: PluginContext) -> None:
        self._context = context
        context.logger.info("[RenamePlugin] Loaded")

    async def on_unload(self) -> None:
        if self._context:
            self._context.logger.info("[RenamePlugin] Unloaded")
        self._context = None
