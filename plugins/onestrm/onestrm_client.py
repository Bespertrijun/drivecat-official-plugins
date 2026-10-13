"""The supplied demo's quick_scan protocol; HTTP acceptance is not STRM completion."""

import asyncio
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp


@dataclass(frozen=True)
class Result:
    ok: bool
    kind: str = ""
    error: str = ""
    http_status: int | None = None
    retryable: bool = False
    retry_at: float | None = None


def retry_after(value, now):
    if not value:
        return None
    try:
        delay = float(value)
        return now + max(0, delay) if math.isfinite(delay) else None
    except ValueError:
        try:
            return max(now, parsedate_to_datetime(value).timestamp())
        except (ValueError, TypeError, OverflowError):
            return None


async def notify(config, path):
    headers, params = {}, {}
    if config["token"]:
        if config["auth_mode"] == "query":
            params["token"] = config["token"]
        else:
            headers["Authorization"] = "Bearer " + config["token"]
    try:
        # Never borrow a drive session, inherit proxies, follow redirects, or log responses.
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config["timeout_seconds"]),
                                  trust_env=False) as session,
            session.post(config["base_url"] + "/webhook/quick_scan", params=params,
                         json={"paths": [path]}, headers=headers, allow_redirects=False) as response,
        ):
            code = response.status
            if 200 <= code < 300:
                return Result(True, http_status=code)
            kind = ("auth" if code in (401, 403) else "rate_limit" if code == 429
                    else "server" if code >= 500 else "http")
            return Result(False, kind, f"OneStrm HTTP {code}", code,
                          code == 429 or code >= 500,
                          retry_after(response.headers.get("Retry-After"), time.time()) if code == 429 else None)
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        return Result(False, "timeout", "OneStrm 请求超时", retryable=True)
    except aiohttp.ClientSSLError:
        return Result(False, "tls", "OneStrm TLS 校验失败，请检查证书")
    except aiohttp.ClientConnectionError:
        return Result(False, "network", "OneStrm 网络连接失败", retryable=True)
    except (aiohttp.ClientError, ValueError):
        return Result(False, "configuration", "OneStrm 请求无效，请检查服务地址与鉴权设置")


async def probe_network(base_url):
    """TCP handshake only, following the demo; does not send a scan or test the token."""
    writer = None
    try:
        parsed = urlsplit(base_url)
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), 5)
        return {"ok": True, "message": "TCP 连接成功；尚未验证鉴权或扫描接口"}
    except (OSError, asyncio.TimeoutError, ValueError):
        return {"ok": False, "message": "TCP 连接失败，请检查地址、端口和网络"}
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass
