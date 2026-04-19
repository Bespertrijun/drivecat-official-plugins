"""
打包脚本：扫描 plugins/ 下所有插件，生成 dist/index.json + zip 包 + 签名。
"""

import hashlib
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
DIST_DIR = ROOT / "dist"
PRIVATE_KEY_PATH = ROOT / "keys" / "private.pem"


def _load_private_key():
    """加载私钥：优先环境变量 SIGNING_KEY，其次 keys/private.pem。"""
    import os
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem_data = os.environ.get("SIGNING_KEY")
    if pem_data:
        return load_pem_private_key(pem_data.encode(), password=None)

    if PRIVATE_KEY_PATH.exists():
        return load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)

    print("  no signing key found, skipping signing")
    return None


def _sign(data: bytes, private_key) -> bytes:
    """对 sha256(data) 做 Ed25519 签名。"""
    digest = hashlib.sha256(data).digest()
    return private_key.sign(digest)


def _get_changelog(plugin_dir: Path, max_entries: int = 20) -> str:
    """从 git log 自动生成 changelog。取该插件目录下最近的 commit 摘要。"""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_entries}", "--pretty=format:%s",
             "--", str(plugin_dir)],
            capture_output=True, text=True, cwd=ROOT,
            encoding="utf-8", errors="replace",
        )
        stdout = result.stdout or ""
        if result.returncode != 0 or not stdout.strip():
            return ""
        lines = stdout.strip().splitlines()
        return "\n".join(f"- {line}" for line in lines)
    except FileNotFoundError:
        # git 不可用
        return ""


def build():
    DIST_DIR.mkdir(exist_ok=True)
    private_key = _load_private_key()
    index_plugins = []

    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        manifest_path = plugin_dir / "manifest.json"
        if not manifest_path.exists():
            continue

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        plugin_id = plugin_dir.name
        version = manifest["version"]

        # 打 zip
        pkg_dir = DIST_DIR / "packages" / plugin_id / version
        pkg_dir.mkdir(parents=True, exist_ok=True)
        zip_path = pkg_dir / "plugin.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 打包插件自身文件（排除 _shared/ dev 副本，由下方统一注入）
            for file in plugin_dir.rglob("*"):
                rel = file.relative_to(plugin_dir)
                if file.is_file() and "__pycache__" not in rel.parts and "_shared" not in rel.parts:
                    zf.write(file, rel)

            # 打包 _shared/ 共享资源（如 sdk.js），使 ui/ 中的相对引用能解析
            shared_dir = PLUGINS_DIR / "_shared"
            if shared_dir.exists():
                for file in shared_dir.rglob("*"):
                    if file.is_file():
                        zf.write(file, Path("_shared") / file.relative_to(shared_dir))

        # 签名
        sig_url = ""
        if private_key:
            sig = _sign(zip_path.read_bytes(), private_key)
            sig_path = pkg_dir / "plugin.sig"
            sig_path.write_bytes(sig)
            sig_url = f"packages/{plugin_id}/{version}/plugin.sig"
            print(f"  [signed] {plugin_id} v{version}")
        else:
            print(f"  [no-sig] {plugin_id} v{version}")

        # 图标
        icon_url = ""
        icon_field = manifest.get("icon", "")
        if icon_field:
            icon_src = plugin_dir / icon_field
            if icon_src.exists():
                icon_dest_dir = DIST_DIR / "packages" / plugin_id
                icon_dest = icon_dest_dir / icon_src.name
                import shutil
                shutil.copy2(icon_src, icon_dest)
                icon_url = f"packages/{plugin_id}/{icon_src.name}"
                print(f"  [icon]   {plugin_id} → {icon_url}")

        # 构建 index 条目
        entry = {
            "name": manifest["name"],
            "version": version,
            "author": manifest.get("author", ""),
            "description": manifest.get("description", ""),
            "source_url": manifest.get("source_url", ""),
            "permissions": manifest.get("permissions", []),
            "changelog": manifest.get("changelog") or _get_changelog(plugin_dir),
            "download_url": f"packages/{plugin_id}/{version}/plugin.zip",
            "signature_url": sig_url,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if icon_url:
            entry["icon_url"] = icon_url
        index_plugins.append(entry)

    # 写 index.json
    index = {"plugins": index_plugins}
    index_path = DIST_DIR / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\n Built {len(index_plugins)} plugin(s) -> {DIST_DIR}")


def dev_sync():
    """将 _shared/ 复制到每个插件目录，使本地开发时相对路径可用。"""
    import shutil

    shared_dir = PLUGINS_DIR / "_shared"
    if not shared_dir.exists():
        print("  _shared/ not found, nothing to sync")
        return

    synced = 0
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not (plugin_dir / "manifest.json").exists():
            continue

        target = plugin_dir / "_shared"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(shared_dir, target)
        synced += 1

    print(f"\n Synced _shared/ -> {synced} plugin(s)")


if __name__ == "__main__":
    import sys

    if "--dev" in sys.argv:
        dev_sync()
    else:
        build()
