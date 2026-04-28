"""
Plugin Dev Runtime — 主入口。

用法：
    python devrt/server.py plugins/rename [--port 9000]

功能：
    1. 动态加载插件 main.py，调用 on_load 挂载路由
    2. 提供 Mock Drive API（/api/drives/、/api/drives/{id}/files）
    3. 提供 FileProxy 沙箱（devrt_data/{plugin_id}/）
    4. 提供宿主模拟页面（host.html + iframe postMessage 协议）
"""

import argparse
import io
import os
import importlib.util
import json
import sys
import types
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

# ── 路径锚点 ──

ROOT = Path(__file__).resolve().parent.parent  # drivecat-official-plugins/
DEVRT_DIR = Path(__file__).resolve().parent     # devrt/
PLUGINS_DIR = ROOT / "plugins"
DATA_DIR = ROOT / "devrt_data"

# 确保仓库根目录在 sys.path 中，使 `from devrt.xxx import ...` 可用
_root_str = str(ROOT)
if _root_str not in sys.path:
    sys.path.insert(0, _root_str)


def create_app(plugin_dir: Path) -> FastAPI:
    """创建 FastAPI 应用并加载插件。"""

    # ── 读取 manifest ──
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"  ✗ manifest.json not found in {plugin_dir}")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    plugin_name = manifest.get("name", plugin_dir.name)
    permissions = manifest.get("permissions", [])
    source_url = manifest.get("source_url", "")

    # plugin_id：与生产环境一致的派生逻辑
    from devrt.stubs import derive_plugin_id
    if source_url and plugin_name:
        plugin_id = derive_plugin_id(source_url, plugin_name)
    else:
        plugin_id = plugin_dir.name

    print(f"  Plugin:      {plugin_name}")
    print(f"  Plugin ID:   {plugin_id}")
    print(f"  Permissions: {permissions}")

    # ── 模块注入：让 `from app.plugin.base import ...` 指向 stubs ──
    import devrt.stubs as stubs

    app_mod = types.ModuleType("app")
    app_mod.__path__ = []
    plugin_mod = types.ModuleType("app.plugin")
    plugin_mod.__path__ = []

    sys.modules["app"] = app_mod
    sys.modules["app.plugin"] = plugin_mod
    sys.modules["app.plugin.base"] = stubs

    # 同时注入 app.drives.base（提供 FileInfo）
    drives_mod = types.ModuleType("app.drives")
    drives_mod.__path__ = []
    drives_base_mod = types.ModuleType("app.drives.base")
    drives_base_mod.FileInfo = stubs.FileInfo
    sys.modules["app.drives"] = drives_mod
    sys.modules["app.drives.base"] = drives_base_mod

    # ── 插件目录加入 sys.path ──
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.insert(0, plugin_dir_str)

    # ── 创建 FastAPI ──
    app = FastAPI(title=f"DevRT — {plugin_name}")

    # ── 创建 MockDrive ──
    from devrt.mock_drive import MockDrive
    mock_drive = MockDrive()

    # ── 创建 PluginContext ──
    from loguru import logger
    context = stubs.PluginContext(
        plugin_id=plugin_id,
        permissions=permissions,
        app=app,
        data_dir=str(DATA_DIR),
        logger=logger,
        mock_drive=mock_drive,
    )

    # ── 动态加载插件 main.py ──
    main_py = plugin_dir / "main.py"
    if not main_py.exists():
        print(f"  ✗ main.py not found in {plugin_dir}")
        sys.exit(1)

    spec_name = f"_devrt_plugin_{plugin_dir.name}"
    spec = importlib.util.spec_from_file_location(spec_name, main_py)
    if spec is None or spec.loader is None:
        print(f"  ✗ Cannot load {main_py}")
        sys.exit(1)

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec_name] = module
    spec.loader.exec_module(module)

    # 找 PluginInterface 子类
    plugin_cls = None
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if (isinstance(obj, type) and issubclass(obj, stubs.PluginInterface)
                and obj is not stubs.PluginInterface):
            plugin_cls = obj
            break

    if plugin_cls is None:
        print(f"  ✗ No PluginInterface subclass found in {main_py}")
        sys.exit(1)

    plugin_instance = plugin_cls()
    print(f"  Entry:       {plugin_cls.__name__}")

    # ── startup 事件：异步调用 on_load ──
    @app.on_event("startup")
    async def _mount_plugin():
        await plugin_instance.on_load(context)
        print(f"  ✓ Plugin loaded, routes mounted")
        for r in context._registered_routers:
            print(f"    → {r}")

    # ── DevRT 自身路由 ──

    # 宿主模拟页面
    @app.get("/", response_class=HTMLResponse)
    async def host_page():
        host_html = DEVRT_DIR / "host.html"
        # 注入运行时变量
        html = host_html.read_text(encoding="utf-8")
        html = html.replace("__PLUGIN_ID__", plugin_id)
        html = html.replace("__PLUGIN_NAME__", plugin_name)
        html = html.replace("__PLUGIN_VERSION__", manifest.get("version", "dev"))
        return HTMLResponse(content=html)

    # Mock Drives API — 裸数组
    @app.get("/api/drives/")
    async def list_drives():
        return JSONResponse(content=[
            {"id": 1, "name": "测试网盘", "drive_type": "mock"},
        ])

    # Mock Files API — {files: [...]}
    @app.get("/api/drives/{drive_id}/files")
    async def list_files(drive_id: int, parent_id: str = "0"):
        return JSONResponse(content={
            "files": mock_drive.to_dict_list(parent_id)
        })

    # 插件 UI 静态文件
    @app.get("/devrt/plugin-ui/{file_path:path}")
    async def plugin_ui_static(file_path: str):
        # 先查插件目录
        target = plugin_dir / file_path
        if target.is_file():
            return FileResponse(target)
        # 再查 _shared/
        shared = PLUGINS_DIR / file_path
        if shared.is_file():
            return FileResponse(shared)
        return JSONResponse(status_code=404, content={"detail": f"Not found: {file_path}"})

    return app


def main():
    # Windows 终端 UTF-8 输出
    if os.name == 'nt':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description="🐱 Plugin Dev Runtime — 插件本地开发环境",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python devrt/server.py plugins/rename
  python devrt/server.py plugins/rename --port 8080
        """,
    )
    parser.add_argument("plugin_dir", help="插件目录路径（如 plugins/rename）")
    parser.add_argument("--port", type=int, default=9000, help="服务端口（默认 9000）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")

    args = parser.parse_args()

    plugin_dir = (ROOT / args.plugin_dir).resolve()
    if not plugin_dir.exists():
        # 也允许绝对路径
        plugin_dir = Path(args.plugin_dir).resolve()
    if not plugin_dir.exists():
        print(f"  ✗ Plugin directory not found: {args.plugin_dir}")
        sys.exit(1)

    print()
    print("  🐱 Plugin Dev Runtime")
    print("  ─────────────────────")

    app = create_app(plugin_dir)

    print()
    print(f"  → http://{args.host}:{args.port}")
    print()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
