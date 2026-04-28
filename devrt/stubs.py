"""
Plugin Dev Runtime — 插件 SDK 存根。

从 DriveCat app/plugin/base.py 精简提取，零外部依赖（不 import 任何 DriveCat 内部模块）。
插件 main.py 中 `from app.plugin.base import ...` 通过 sys.modules 注入指向本模块。
"""

import hashlib
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ── ID 派生 ──


def derive_plugin_id(source_url: str, name: str) -> str:
    """从 source_url + name 派生 plugin_id。"""
    raw = f"{source_url}:{name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── 数据模型 ──


class PluginUIHook(BaseModel):
    """描述插件在宿主中的一个 UI 挂载点。"""
    position: str
    label: str
    icon: str = ""
    match: Dict[str, Any] = {}


class PluginUIManifest(BaseModel):
    """插件 UI 嵌入声明。"""
    mode: str = "iframe"
    entry: str = "ui/index.html"
    hooks: List[PluginUIHook] = []


class PluginMeta(BaseModel):
    """插件元信息（manifest.json 映射）。"""
    name: str
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    hooks: List[str] = []
    permissions: List[str] = []
    source: str = "community"
    entry: str = "main.Plugin"
    source_url: str = ""
    plugin_id: str = ""
    icon: str = ""
    ui: Optional[PluginUIManifest] = None


# ── 钩子上下文 ──


class HookContext(BaseModel):
    """传递给钩子 handler 的上下文载体。"""
    model_config = {"arbitrary_types_allowed": True}

    hook_name: str
    data: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class HookResult(BaseModel):
    """钩子分发的聚合结果。"""
    success: bool = True
    modified: bool = False
    data: Dict[str, Any] = {}
    errors: List[str] = []


# ── 文件信息 ──


class FileInfo:
    """精简版文件信息，兼容 DriveCat drives.base.FileInfo 接口。"""

    def __init__(
        self,
        id: str,
        name: str,
        size: int = 0,
        is_dir: bool = False,
        parent_id: str = "0",
        modified_time: Optional[str] = None,
    ):
        self.id = id
        self.name = name
        self.size = size
        self.is_dir = is_dir
        self.parent_id = parent_id
        self.modified_time = modified_time
        self.modified_at = modified_time


# ── FileProxy（完整实现，从 base.py 复制） ──


class FileProxy:
    """
    权限受限的文件系统代理。

    将所有文件操作限制在 plugin_data/{plugin_id}/ 目录内，
    通过 resolve() + is_relative_to 检查防止路径遍历攻击。

    权限：
        fs.read  → read_bytes / read_text / list_dir / exists
        fs.write → write_bytes / write_text / mkdir / delete
    """

    def __init__(self, root_dir: Path, can_read: bool, can_write: bool):
        self._root = root_dir.resolve()
        self._can_read = can_read
        self._can_write = can_write
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel_path: str) -> Path:
        """将相对路径解析为绝对路径，并检查是否在允许的根目录内。"""
        if Path(rel_path).is_absolute():
            raise PermissionError(f"Absolute paths are not allowed: {rel_path}")
        target = (self._root / rel_path).resolve()
        if not target.is_relative_to(self._root):
            raise PermissionError(
                f"Path traversal blocked: '{rel_path}' escapes plugin data directory"
            )
        return target

    @property
    def root(self) -> str:
        """返回插件数据目录的绝对路径（只读）。"""
        return str(self._root)

    # ── 读操作 ──

    def read_bytes(self, path: str) -> bytes:
        if not self._can_read:
            raise PermissionError("Plugin does not have 'fs.read' permission")
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        if not self._can_read:
            raise PermissionError("Plugin does not have 'fs.read' permission")
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_text(encoding=encoding)

    def list_dir(self, path: str = ".") -> List[str]:
        if not self._can_read:
            raise PermissionError("Plugin does not have 'fs.read' permission")
        target = self._resolve(path)
        if not target.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        return sorted([p.name for p in target.iterdir()])

    def exists(self, path: str) -> bool:
        if not self._can_read:
            raise PermissionError("Plugin does not have 'fs.read' permission")
        return self._resolve(path).exists()

    # ── 写操作 ──

    def write_bytes(self, path: str, data: bytes) -> None:
        if not self._can_write:
            raise PermissionError("Plugin does not have 'fs.write' permission")
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        if not self._can_write:
            raise PermissionError("Plugin does not have 'fs.write' permission")
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding=encoding)

    def mkdir(self, path: str) -> None:
        if not self._can_write:
            raise PermissionError("Plugin does not have 'fs.write' permission")
        self._resolve(path).mkdir(parents=True, exist_ok=True)

    def delete(self, path: str) -> None:
        if not self._can_write:
            raise PermissionError("Plugin does not have 'fs.write' permission")
        target = self._resolve(path)
        if target.is_dir():
            raise PermissionError("Cannot delete directories via FileProxy. Use delete_dir() instead.")
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        target.unlink()

    def delete_dir(self, path: str) -> None:
        if not self._can_write:
            raise PermissionError("Plugin does not have 'fs.write' permission")
        target = self._resolve(path)
        if target == self._root:
            raise PermissionError("Cannot delete plugin data root directory")
        if not target.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        shutil.rmtree(target)


# ── 静默钩子分发器 ──


class _SilentHookDispatcher:
    """接受 hook 注册，但不触发任何回调。DevRT 专用。"""

    def register(self, hook_name: str, callback: Any, plugin_id: str = None):
        pass

    def unregister_plugin(self, plugin_id: str):
        pass


# ── 插件上下文（DevRT 版） ──


class PluginContext:
    """
    DevRT 版 PluginContext。

    与生产版行为一致的能力：
        - get_fs()          → 真实 FileProxy 沙箱
        - register_router() → 真实 FastAPI 路由挂载
        - get_drive()       → MockDrive 代理
    不可用的能力：
        - get_db()          → NotImplementedError
        - register_job()    → 静默忽略
    """

    def __init__(
        self,
        plugin_id: str,
        permissions: List[str],
        app: Any,
        data_dir: str,
        logger: Any,
        mock_drive: Any,
    ):
        self.plugin_id = plugin_id
        self.permissions = permissions
        self.logger = logger
        self.hooks = _SilentHookDispatcher()
        self._app = app
        self._data_dir = data_dir
        self._mock_drive = mock_drive
        self._registered_routers: list = []
        self._registered_jobs: list = []

    def get_fs(self) -> FileProxy:
        """获取权限受限的文件系统代理。"""
        can_read = "fs.read" in self.permissions
        can_write = "fs.write" in self.permissions
        if not can_read and not can_write:
            raise PermissionError("Plugin does not have 'fs.read' or 'fs.write' permission")
        data_dir = Path(self._data_dir) / self.plugin_id
        return FileProxy(data_dir, can_read, can_write)

    def register_router(self, router: Any, prefix: str = "", tags: Optional[List[str]] = None) -> None:
        """动态注册 API 路由。"""
        safe_prefix = f"/api/plugins/{self.plugin_id}{prefix}"
        final_tags = tags or [self.plugin_id]
        self._app.include_router(router, prefix=safe_prefix, tags=final_tags)
        self._registered_routers.append(safe_prefix)
        if self.logger:
            self.logger.info(f"[DevRT] Mounted routes: {safe_prefix}")

    async def get_drive(self, drive_config_id: int) -> Any:
        """返回 MockDrive（不区分 drive_config_id）。"""
        return self._mock_drive

    def get_db(self) -> Any:
        """DevRT 不提供数据库访问。"""
        raise NotImplementedError(
            "Dev Runtime does not provide database access. Use get_fs() for persistence."
        )

    def register_job(self, *args, **kwargs) -> None:
        """静默忽略定时任务注册。"""
        pass

    def unregister_jobs(self) -> None:
        pass


# ── 插件接口 ──


class PluginInterface(ABC):
    """
    所有插件必须实现的接口。

    生命周期:
        1. __init__()        — 框架实例化
        2. on_load(context)  — 启用时调用，注册钩子
        3. on_unload()       — 禁用/卸载时调用，清理资源
    """

    @abstractmethod
    def get_meta(self) -> PluginMeta:
        """返回插件元信息。"""
        ...

    @abstractmethod
    async def on_load(self, context: PluginContext) -> None:
        """插件启用时调用。在此注册钩子、初始化资源。"""
        ...

    @abstractmethod
    async def on_unload(self) -> None:
        """插件禁用/卸载时调用。在此注销钩子、释放资源。"""
        ...
