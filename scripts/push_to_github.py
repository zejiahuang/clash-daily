#!/usr/bin/env python3
"""把 dist/ 里的产物推回 GitHub（AtomGit 跑 CI 时用）。

为什么需要这一步：AtomGit 没有任何 raw 文本端点（实测 12 种形式全是前端 SPA），
所以订阅地址只能在 GitHub。AtomGit 的流水线负责「在中国大陆附近测速」，
产物通过 GitHub Contents API 写回去。

用法：
    GH_TOKEN=... GITHUB_REPO=owner/repo python scripts/push_to_github.py dist

只用标准库，不需要额外依赖。任何单文件失败都不影响其它文件，最后统一返回码。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
# 只推这些文件：base64 版是给只认 base64 订阅的客户端用的
WANTED = ("clash.yaml", "clash.base64.txt", "report.json", "report.md", "runner-geo.json")


def api(method: str, path: str, payload=None, token: str = ""):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "atomgit-to-github",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(4):
        req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 404:
                return 404, {}
            last = f"HTTP {exc.code}: {detail[:200]}"
            if exc.code in (401, 403) and "rate limit" not in detail.lower():
                break
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5 * (attempt + 1))
    return 0, {"error": last}


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPO") or ""
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")

    if not token or not repo:
        print("跳过：没有 GH_TOKEN / GITHUB_REPO，产物只留在 AtomGit 仓库里（不能订阅）")
        return 0
    if not src.is_dir():
        print(f"失败：目录不存在 {src}")
        return 1

    files = [(name, src / name) for name in WANTED if (src / name).is_file()]
    if not files:
        print(f"失败：{src} 里没有可推的文件")
        return 1

    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))
    ok, failed = 0, []
    for name, path in files:
        data = path.read_bytes()
        target = f"dist/{name}"
        code, info = api("GET", f"/repos/{repo}/contents/{target}?ref={BRANCH}", token=token)
        payload = {
            "message": f"chore(nodes): AtomGit 测速结果 {stamp} 北京时间",
            "content": base64.b64encode(data).decode(),
            "branch": BRANCH,
        }
        if code == 200 and isinstance(info, dict) and info.get("sha"):
            payload["sha"] = info["sha"]
            code, info = api("PUT", f"/repos/{repo}/contents/{target}", payload, token=token)
        else:
            code, info = api("PUT", f"/repos/{repo}/contents/{target}", payload, token=token)
        if code in (200, 201):
            ok += 1
            print(f"  ok   {target} ({len(data)} B)")
        else:
            failed.append((target, info))
            print(f"  FAIL {target} -> {code} {str(info)[:160]}")

    print(f"\n推送完成：成功 {ok}，失败 {len(failed)}")
    for target, info in failed:
        print(f"  ! {target}: {str(info)[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
