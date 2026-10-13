"""Configuration follows the existing plugin atomic-file storage pattern."""

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class Config(BaseModel):
    enabled: bool = False
    drive_id: int | None = Field(default=None, gt=0)
    base_url: str = ""
    token: str | None = None
    clear_token: bool = False
    auth_mode: Literal["bearer", "query"] = "bearer"
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    quiet_seconds: int = Field(default=10, ge=1, le=60)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(ord(c) < 32 for c in value)):
            raise ValueError("请填写 HTTP(S) 服务地址，Token 请单独填写")
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("端口无效") from None
        return value

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if value and any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Token 不能包含换行或控制字符")
        return value

    @model_validator(mode="after")
    def complete(self):
        if self.enabled and (not self.drive_id or not self.base_url):
            raise ValueError("启用前请选择 115 网盘并填写 OneStrm 地址")
        return self


def load_config(directory):
    path = Path(directory) / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = Config.model_validate(raw).model_dump(exclude={"clear_token"})
    config["token"] = config["token"] or ""
    config["changed_at"] = float(raw.get("changed_at", 0))
    config["revision"] = int(raw.get("revision", 0))
    config["auth_revision"] = int(raw.get("auth_revision", 0))
    config["drive_name"] = str(raw.get("drive_name", ""))
    return config


def public_config(config):
    return {**{k: v for k, v in config.items() if k != "token"},
            "token_set": bool(config.get("token"))}


def target_key(config):
    if not config.get("drive_id") or not config.get("base_url"):
        return ""
    raw = json.dumps([config["drive_id"], config["base_url"]], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def write_config(directory, config):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as file:
            name = file.name
            json.dump(config, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, directory / "config.json")
        # Also persist the rename, so a committed settings update survives a crash.
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def cloud_path(value, *, file=False):
    if (not isinstance(value, str) or not value.startswith("/")
            or value.startswith("//") or any(ord(c) < 32 for c in value)
            or ".." in value.split("/") or (file and value.split("/")[-1] in ("", "."))):
        raise ValueError("需要有效的云端绝对路径")
    path = PurePosixPath(value)
    return str(path.parent if file else path)
