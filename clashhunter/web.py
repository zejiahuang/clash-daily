"""极简 HTTP 客户端：只用标准库，带 UA / Referer / 重试 / gzip / GitHub 镜像回退。

源站对裸 urllib 会返回 403，必须带浏览器 UA 和 Referer，这一点是实测出来的。
"""

from __future__ import annotations

import gzip
import json
import os
import random
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlsplit

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 把完整 URL 拼在后面的直通代理 —— 取到的是同一份字节，不是缓存副本。
# 为什么需要：raw.githubusercontent.com 在部分网络下 TCP 能连、TLS 被中途 RST
# （实测：0.09s 连上，随后 ConnectionResetError(10054)），换一个前缀就正常。
# 顺序 = 尝试顺序。`GH_MIRROR_PREFIX=""` 可整体关掉回退。
DEFAULT_MIRRORS = ("https://gh-proxy.com/", "https://cdn.akaere.online/")

# 只对这几个主机名回退：别的站点没有对应镜像，乱拼只会多一次无谓的 404。
_MIRROR_HOSTS = (
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "github.com",
    "objects.githubusercontent.com",
)


class FetchError(Exception):
    pass


class _Missing(Exception):
    """404：文件本身不存在，换镜像也一样，不必再试。"""


def mirror_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("GH_MIRROR_PREFIX")
    if raw is None:
        return DEFAULT_MIRRORS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def candidate_urls(url: str) -> list[str]:
    """直连优先，失败再依次用镜像前缀拼。非 GitHub 主机原样返回。"""
    host = (urlsplit(url).hostname or "").lower()
    if host not in _MIRROR_HOSTS:
        return [url]
    return [url] + [p.rstrip("/") + "/" + url for p in mirror_prefixes()]


def _get_text(url: str, hdrs: dict, *, timeout: int, retries: int,
              data: bytes | None, method: str | None) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, "replace")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise _Missing(f"{url}: 404") from exc
            # 4xx 除 429 外重试没有意义
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt + 1 < retries:
            time.sleep(1.2 * (attempt + 1) + random.random())
    raise FetchError(f"{url}: {type(last).__name__}: {last}") from last


def fetch(
    url: str,
    *,
    referer: str | None = None,
    timeout: int = 30,
    retries: int = 3,
    headers: dict | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> str:
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update(headers)

    last: Exception | None = None
    for candidate in candidate_urls(url):
        try:
            return _get_text(candidate, hdrs, timeout=timeout, retries=retries,
                             data=data, method=method)
        except _Missing as exc:
            last = exc
            break          # 404：文件本身不存在，换镜像也一样
        except FetchError as exc:
            last = exc
    # 每条 FetchError 的正文里都带着它自己试的那个地址（也就是原始地址或镜像地址），
    # 所以这里不再套一层前缀 —— 否则错误信息里会出现两遍同一个 URL。
    raise FetchError(str(last)) from last


def fetch_json(url: str, payload=None, **kwargs):
    if payload is None:
        return json.loads(fetch(url, **kwargs))
    body = json.dumps(payload).encode()
    return json.loads(
        fetch(url, data=body, method="POST",
              headers={"Content-Type": "application/json"}, **kwargs)
    )


def origin_of(url: str) -> str:
    """https://a.b/c/d -> https://a.b/"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"
