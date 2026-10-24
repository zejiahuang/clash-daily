"""web.py 的 GitHub 镜像回退（离线，不联网）。

为什么值得单独钉住：回退是一条**只在失败时才走**的分支，正常跑一遍永远是绿的。
一旦它写错（前缀拼错、把 404 也当可回退），现象是「某些源站静默少几个节点」，
而不是报错 —— 所以这里把「直连失败后才用镜像」和「404 不许换镜像」两条都钉住。
"""

from __future__ import annotations

import email.message
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter import web as webmod  # noqa: E402

RAW = "https://raw.githubusercontent.com/o/r/main/x.yaml"


class _Resp:
    """urlopen 的返回值只需要 read() / headers / 上下文管理。"""

    def __init__(self, body: bytes):
        self._body = body
        self.headers = email.message.Message()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_urlopen(calls: list[str], *, body: bytes = b"hello", status: int = 0):
    """status=0 走成功分支；否则抛对应 HTTPError。"""

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if status:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, None)
        if req.full_url.startswith("https://gh-proxy.com/"):
            return _Resp(body)
        raise urllib.error.URLError("connection reset")

    return fake


def main() -> int:
    problems: list[str] = []

    # 1. 非 GitHub 主机不该被拼镜像
    got = webmod.candidate_urls("https://example.com/a.yaml")
    if got != ["https://example.com/a.yaml"]:
        problems.append(f"非 GitHub 主机被改了：{got}")

    # 2. GitHub 主机：直连第一，后面每个镜像都只是「前缀 + 原地址」
    urls = webmod.candidate_urls(RAW)
    if urls[0] != RAW:
        problems.append(f"第一候选不是直连：{urls[0]}")
    if len(urls) != 1 + len(webmod.DEFAULT_MIRRORS):
        problems.append(f"候选数 {len(urls)}，应为 1+{len(webmod.DEFAULT_MIRRORS)}")
    for extra in urls[1:]:
        if not extra.endswith(RAW):
            problems.append(f"镜像地址没保留原地址：{extra}")

    # 3. 环境变量覆盖 / 关闭
    with patch.dict(os.environ, {"GH_MIRROR_PREFIX": "https://m1/, https://m2"}):
        got = webmod.candidate_urls(RAW)
    if got != [RAW, "https://m1/" + RAW, "https://m2/" + RAW]:
        problems.append(f"GH_MIRROR_PREFIX 覆盖失效：{got}")
    with patch.dict(os.environ, {"GH_MIRROR_PREFIX": ""}):
        got = webmod.candidate_urls(RAW)
    if got != [RAW]:
        problems.append(f"GH_MIRROR_PREFIX 空串应关闭回退：{got}")

    # 4. 直连失败 → 真的会去试镜像，并返回镜像的正文
    calls: list[str] = []
    with patch("urllib.request.urlopen", _fake_urlopen(calls)):
        text = webmod.fetch(RAW, retries=1)
    if text != "hello":
        problems.append(f"回退后拿到的正文不对：{text!r}")
    if not calls or calls[0] != RAW:
        problems.append(f"第一次请求不是直连：{calls}")
    if len(calls) < 2 or not calls[1].startswith("https://gh-proxy.com/"):
        problems.append(f"直连失败后没走镜像：{calls}")

    # 5. 404 = 文件不存在，换镜像也一样，不许回退
    calls = []
    with patch("urllib.request.urlopen", _fake_urlopen(calls, status=404)):
        try:
            webmod.fetch(RAW, retries=1)
        except webmod.FetchError:
            pass
        else:
            problems.append("404 应该抛 FetchError")
    if calls != [RAW]:
        problems.append(f"404 不该换镜像：{calls}")

    # 6. 全失败时错误信息里要能看见原始地址（否则不知道是哪个源站）
    def always_fail(req, timeout=None):
        raise urllib.error.URLError("nope")

    with patch("urllib.request.urlopen", always_fail):
        try:
            webmod.fetch(RAW, retries=1)
        except webmod.FetchError as exc:
            if RAW not in str(exc):
                problems.append(f"错误信息丢了原始地址：{exc}")
        else:
            problems.append("全失败应该抛 FetchError")

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ web.py 镜像回退通过：直连优先 / 失败回退 / 404 不回退 / 前缀可覆盖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
