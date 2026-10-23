"""极简 HTTP 客户端：只用标准库，带 UA / Referer / 重试 / gzip。

源站对裸 urllib 会返回 403，必须带浏览器 UA 和 Referer，这一点是实测出来的。
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.request
import zlib

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    pass


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
            # 4xx 除 429 外重试没有意义
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt + 1 < retries:
            time.sleep(1.2 * (attempt + 1) + random.random())
    raise FetchError(f"{url}: {type(last).__name__}: {last}")


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
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"
