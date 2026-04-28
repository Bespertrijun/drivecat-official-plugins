"""
打包脚本：扫描 plugins/ 下所有插件，生成 dist/index.json + zip 包 + 签名。

版本号策略：
  - 从 git tag 读取，tag 格式：{plugin_dir_name}/v{semver}
    例：rename/v1.0.0, rename/v1.0.1
  - 没有 tag 时默认 0.0.0
  - 多版本保留：dist/packages/{id}/{version}/plugin.zip
  - 历史版本通过 git archive 从对应 tag 重建，保证回退到任意旧版可用

Changelog 策略：
  1. manifest.json 中手写 changelog → 最高优先级
  2. 两个相邻 tag 之间的 git log → 自动生成
  3. 都没有 → 空字符串
"""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

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


# ── 版本号：基于 git tag ──


def _get_tags_for_plugin(plugin_name: str) -> List[Tuple[str, str]]:
    """
    获取某插件的所有 git tags，按语义版本降序排列。

    Tag 格式：{plugin_name}/v{semver}，如 rename/v1.0.0
    返回：[(tag_name, version_str), ...] 降序排列
    """
    prefix = f"{plugin_name}/v"
    try:
        result = subprocess.run(
            ["git", "tag", "--list", f"{prefix}*", "--sort=-v:refname"],
            capture_output=True, text=True, cwd=ROOT,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return []
        tags = []
        for line in result.stdout.strip().splitlines():
            tag = line.strip()
            if tag.startswith(prefix):
                version = tag[len(prefix):]
                tags.append((tag, version))
        return tags
    except FileNotFoundError:
        return []


def _get_latest_version(plugin_name: str) -> str:
    """获取插件的最新版本号。没有 tag 时返回 '0.0.0'。"""
    tags = _get_tags_for_plugin(plugin_name)
    return tags[0][1] if tags else "0.0.0"


def _get_all_versions(plugin_name: str) -> List[str]:
    """获取插件的所有版本号列表（降序）。"""
    return [v for _, v in _get_tags_for_plugin(plugin_name)]


# ── Changelog：两个 tag 之间的 git log ──


def _get_changelog(plugin_name: str, plugin_dir: Path, max_entries: int = 20) -> str:
    """
    从 git log 自动生成 changelog。

    策略：取当前 tag 和上一个 tag 之间的 commit 摘要。
    如果只有一个 tag（首个版本），取该 tag 之前所有 commits。
    """
    tags = _get_tags_for_plugin(plugin_name)
    if not tags:
        # 没有任何 tag，取最近 N 条 commit
        return _git_log_range(None, "HEAD", plugin_dir, max_entries)

    current_tag = tags[0][0]
    if len(tags) >= 2:
        prev_tag = tags[1][0]
        # 两个 tag 之间的 commits
        return _git_log_range(prev_tag, current_tag, plugin_dir, max_entries)
    else:
        # 只有一个 tag，取该 tag 及之前的所有 commits
        return _git_log_range(None, current_tag, plugin_dir, max_entries)


def _get_changelog_for_version(
    plugin_name: str, version: str, plugin_dir: Path, max_entries: int = 20
) -> str:
    """获取指定版本的 changelog（该版本 tag 到上一个 tag 之间的 commits）。"""
    tags = _get_tags_for_plugin(plugin_name)
    tag_versions = [v for _, v in tags]

    target_tag = f"{plugin_name}/v{version}"
    if version not in tag_versions:
        return ""

    idx = tag_versions.index(version)
    if idx + 1 < len(tags):
        prev_tag = tags[idx + 1][0]
        return _git_log_range(prev_tag, target_tag, plugin_dir, max_entries)
    else:
        return _git_log_range(None, target_tag, plugin_dir, max_entries)


def _git_log_range(
    from_ref: Optional[str], to_ref: str, plugin_dir: Path, max_entries: int
) -> str:
    """获取 from_ref..to_ref 之间，限定在 plugin_dir 下的 commit 摘要。"""
    try:
        if from_ref:
            range_spec = f"{from_ref}..{to_ref}"
        else:
            range_spec = to_ref

        result = subprocess.run(
            ["git", "log", range_spec, f"--max-count={max_entries}",
             "--pretty=format:%s", "--", str(plugin_dir)],
            capture_output=True, text=True, cwd=ROOT,
            encoding="utf-8", errors="replace",
        )
        stdout = result.stdout or ""
        if result.returncode != 0 or not stdout.strip():
            return ""
        lines = stdout.strip().splitlines()
        return "\n".join(f"- {line}" for line in lines)
    except FileNotFoundError:
        return ""


# ── 从 git tag 重建 zip ──


_SKIP_PARTS = {"__pycache__", "_shared", ".git"}


def _pack_zip_for_tag(plugin_id: str, version: str, tag_name: str, pkg_dir: Path) -> Optional[Path]:
    """
    从 git tag 抽取插件代码 + _shared 共享资源，打包成 plugin.zip。

    使用 git archive，不污染工作树。manifest.json 的 version 字段被注入为 tag 版本号。
    返回生成的 zip 路径；失败返回 None。
    """
    pkg_dir.mkdir(parents=True, exist_ok=True)
    zip_path = pkg_dir / "plugin.zip"

    plugin_archive = subprocess.run(
        ["git", "archive", "--format=tar", tag_name, "--", f"plugins/{plugin_id}/"],
        capture_output=True, cwd=ROOT,
    )
    if plugin_archive.returncode != 0 or not plugin_archive.stdout:
        return None

    manifest_blob = subprocess.run(
        ["git", "show", f"{tag_name}:plugins/{plugin_id}/manifest.json"],
        capture_output=True, cwd=ROOT,
    )
    if manifest_blob.returncode != 0:
        return None
    manifest = json.loads(manifest_blob.stdout.decode("utf-8"))
    build_manifest = {**manifest, "version": version}

    # _shared 在该 tag 时可能不存在，失败容忍
    shared_archive = subprocess.run(
        ["git", "archive", "--format=tar", tag_name, "--", "plugins/_shared/"],
        capture_output=True, cwd=ROOT,
    )

    plugin_prefix = f"plugins/{plugin_id}/"
    shared_prefix = "plugins/_shared/"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 插件文件
        with tarfile.open(fileobj=io.BytesIO(plugin_archive.stdout), mode="r") as tar:
            for m in tar.getmembers():
                if not m.isfile() or not m.name.startswith(plugin_prefix):
                    continue
                rel = m.name[len(plugin_prefix):]
                if not rel or rel == "manifest.json":
                    continue
                if any(p in _SKIP_PARTS for p in Path(rel).parts):
                    continue
                f = tar.extractfile(m)
                if f is not None:
                    zf.writestr(rel, f.read())

        # 注入版本号的 manifest
        zf.writestr(
            "manifest.json",
            json.dumps(build_manifest, ensure_ascii=False, indent=2),
        )

        # _shared/ 共享资源
        if shared_archive.returncode == 0 and shared_archive.stdout:
            try:
                with tarfile.open(fileobj=io.BytesIO(shared_archive.stdout), mode="r") as tar:
                    for m in tar.getmembers():
                        if not m.isfile() or not m.name.startswith(shared_prefix):
                            continue
                        rel = m.name[len(shared_prefix):]
                        if not rel:
                            continue
                        f = tar.extractfile(m)
                        if f is not None:
                            zf.writestr(f"_shared/{rel}", f.read())
            except tarfile.ReadError:
                pass

    return zip_path


# ── 构建 ──


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
        all_versions = _get_all_versions(plugin_id)

        if not all_versions:
            print(f"  [skip]   {plugin_id} — no tag found (create one: git tag {plugin_id}/v1.0.0)")
            continue

        # 为每个版本（包括历史）从对应 tag 构建 zip
        signed_versions = set()
        for v in all_versions:
            tag_name = f"{plugin_id}/v{v}"
            pkg_dir = DIST_DIR / "packages" / plugin_id / v
            zip_path = _pack_zip_for_tag(plugin_id, v, tag_name, pkg_dir)
            if zip_path is None:
                print(f"  [fail]   {plugin_id} v{v} — git archive failed")
                continue

            if private_key:
                sig = _sign(zip_path.read_bytes(), private_key)
                (pkg_dir / "plugin.sig").write_bytes(sig)
                signed_versions.add(v)
                print(f"  [signed] {plugin_id} v{v}")
            else:
                print(f"  [no-sig] {plugin_id} v{v}")

        latest_version = all_versions[0]
        sig_url = (
            f"packages/{plugin_id}/{latest_version}/plugin.sig"
            if latest_version in signed_versions
            else ""
        )

        # 图标（取工作树最新文件，不区分版本）
        icon_url = ""
        icon_field = manifest.get("icon", "")
        if icon_field:
            icon_src = plugin_dir / icon_field
            if icon_src.exists():
                icon_dest_dir = DIST_DIR / "packages" / plugin_id
                icon_dest_dir.mkdir(parents=True, exist_ok=True)
                icon_dest = icon_dest_dir / icon_src.name
                import shutil
                shutil.copy2(icon_src, icon_dest)
                icon_url = f"packages/{plugin_id}/{icon_src.name}"
                print(f"  [icon]   {plugin_id} → {icon_url}")

        # changelog: manifest 手写优先，否则从 tag 间 git log 自动生成
        changelog = manifest.get("changelog") or _get_changelog(plugin_id, plugin_dir)

        # 构建 index 条目（指向最新版本）
        entry = {
            "name": manifest["name"],
            "version": latest_version,
            "author": manifest.get("author", ""),
            "description": manifest.get("description", ""),
            "source_url": manifest.get("source_url", ""),
            "permissions": manifest.get("permissions", []),
            "changelog": changelog,
            "download_url": f"packages/{plugin_id}/{latest_version}/plugin.zip",
            "signature_url": sig_url,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        # 历史版本列表（每条带 download_url 和 signature_url）
        if len(all_versions) > 1:
            entry["versions"] = [
                {
                    "version": v,
                    "download_url": f"packages/{plugin_id}/{v}/plugin.zip",
                    "signature_url": (
                        f"packages/{plugin_id}/{v}/plugin.sig"
                        if v in signed_versions
                        else ""
                    ),
                    "changelog": _get_changelog_for_version(plugin_id, v, plugin_dir),
                }
                for v in all_versions
            ]

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
