"""qBittorrent cleanup: exact file matching, with no downloaded-data deletion."""

import http.cookiejar
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath


def absolute_path(value):
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("请填写 Linux / Docker 绝对路径")
    if ".." in value.split("/"):
        raise ValueError("路径不能包含 ..")
    return PurePosixPath("/" + value.lstrip("/")).as_posix()


def mapped_path(local_path, mappings):
    path = PurePosixPath(absolute_path(local_path))
    for mapping in sorted(mappings, key=lambda m: len(PurePosixPath(m["local_path"]).parts), reverse=True):
        source = PurePosixPath(mapping["local_path"])
        if path.is_relative_to(source):
            return (PurePosixPath(mapping["qb_path"]) / path.relative_to(source)).as_posix()
    return None


class QbError(RuntimeError):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def valid_hash(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value))


def torrent_files(torrent, files):
    save = PurePosixPath(absolute_path(torrent.get("save_path")))
    paths = {}
    for file in files:
        name = file.get("name")
        if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or "\x00" in name or ".." in name.split("/"):
            raise ValueError("qB 文件路径无效")
        path = (save / name).as_posix()
        if path in paths or not isinstance(file.get("size"), int) or file["size"] < 0:
            raise ValueError("qB 文件大小缺失或路径重复")
        paths[path] = file["size"]
    return paths


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QbClient:
    def __init__(self, config):
        self.config = config
        self.base = config["base_url"].rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            _NoRedirect(),
        )

    def request(self, endpoint, data=None, query=None, _reauth=True):
        url = self.base + "/api/v2/" + endpoint
        if query:
            url += "?" + urllib.parse.urlencode(query)
        parsed = urllib.parse.urlsplit(self.base)
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode() if data is not None else None,
            headers={"Origin": f"{parsed.scheme}://{parsed.netloc}", "Referer": self.base + "/"},
        )
        try:
            with self.opener.open(req, timeout=self.config["timeout_seconds"]) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and endpoint != "auth/login" and _reauth:
                self.login()
                return self.request(endpoint, data, query, _reauth=False)
            raise QbError(f"qB 返回 HTTP {exc.code}，请检查地址、认证及 WebUI 访问限制",
                          permanent=300 <= exc.code < 500 and exc.code not in (408, 429)) from None
        except (OSError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise QbError("qB TLS 证书验证失败，请修复证书", permanent=True) from None
            raise QbError("无法连接 qB，请检查地址、端口和网络") from None
        except ValueError:
            raise QbError("qB 地址无效", permanent=True) from None

    def login(self):
        result = self.request("auth/login", {
            "username": self.config["username"], "password": self.config["password"],
        })
        if result.strip() != "Ok.":
            raise QbError("qB 登录失败，请检查用户名和密码", permanent=True)

    def json(self, endpoint, query=None):
        try:
            value = json.loads(self.request(endpoint, query=query))
        except (ValueError, UnicodeError):
            raise QbError("qB 返回了无效 JSON") from None
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise QbError("qB 返回了无效列表")
        return value

    def remove(self, hashes):
        if hashes:
            # Never accept 'all', and never ask qB to delete downloaded data.
            if not all(valid_hash(h) for h in hashes):
                raise QbError("无效种子 hash")
            self.request("torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "false"})
