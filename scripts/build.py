"""
打包脚本：扫描 plugins/ 下所有插件，生成 dist/index.json + zip 包 + 签名。
"""

import hashlib
import json
import zipfile
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
            for file in plugin_dir.rglob("*"):
                if file.is_file() and "__pycache__" not in str(file):
                    zf.write(file, file.relative_to(plugin_dir))

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

        # 构建 index 条目
        index_plugins.append({
            "name": manifest["name"],
            "version": version,
            "author": manifest.get("author", ""),
            "description": manifest.get("description", ""),
            "category": manifest.get("category", ""),
            "permissions": manifest.get("permissions", []),
            "download_url": f"packages/{plugin_id}/{version}/plugin.zip",
            "signature_url": sig_url,
            "updated_at": "",
        })

    # 写 index.json
    index = {"plugins": index_plugins}
    index_path = DIST_DIR / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\n Built {len(index_plugins)} plugin(s) -> {DIST_DIR}")


if __name__ == "__main__":
    build()
