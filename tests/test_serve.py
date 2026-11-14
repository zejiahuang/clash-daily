"""订阅 HTTP 站点的离线回归测试（只在 127.0.0.1 上起一个临时端口）。

为什么值得测：这个服务把 `dist/` 直接暴露到公网，一旦路径处理写错，
后果不是「页面打不开」，而是**仓库里的任意文件被读走**。所以核心断言只有两类：
白名单之外的路径必须 404、订阅文件必须逐字节等于磁盘上的那份。
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import serve_dist  # noqa: E402


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def main() -> int:
    problems: list[str] = []

    # 端口合法性：66666 超过 65535，绑不上 —— 这种人眼容易漏的常量必须钉住
    if serve_dist.DEFAULT_PORT > 65535:
        problems.append(f"默认端口 {serve_dist.DEFAULT_PORT} 超过 65535，bind 会直接失败")
    if serve_dist.DEFAULT_PORT != 60066:
        problems.append(f"默认端口应为本机约定的 60066，实得 {serve_dist.DEFAULT_PORT}")

    dist = ROOT / "dist"
    if not (dist / "clash.yaml").is_file():
        # 没有产物时跳过内容断言，但仍验证 404 与页面可打开
        expected = None
    else:
        expected = (dist / "clash.yaml").read_bytes()

    server = serve_dist.make_server("127.0.0.1", 0, dist)
    server.RequestHandlerClass.log_message = lambda *a, **k: None
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _get(port, "/")
        if status != 200:
            problems.append(f"/ 应返回 200，实得 {status}")
        if "text/html" not in headers.get("Content-Type", ""):
            problems.append(f"/ 的 Content-Type 不对：{headers.get('Content-Type')}")
        if "no-store" not in headers.get("Cache-Control", ""):
            problems.append("产物每天更新，页面和订阅都必须 no-store，否则会读到昨天的节点")
        if b"/clash.yaml" not in body or b"/clash.base64.txt" not in body:
            problems.append("首页没有给出两个订阅地址")
        if str(port).encode() not in body:
            problems.append("首页的订阅地址没有带上实际监听端口")

        for path in ("/clash.yaml", "/dist/clash.yaml", "/clash.base64.txt",
                     "/report.json", "/report.md"):
            status, headers, body = _get(port, path)
            if expected is not None and path.endswith("clash.yaml"):
                if status != 200 or body != expected:
                    problems.append(f"{path} 没有逐字节返回 dist/clash.yaml（status={status}）")
                if "text/plain" not in headers.get("Content-Type", ""):
                    problems.append(f"{path} 的 Content-Type 必须是 text/plain，"
                                    f"实得 {headers.get('Content-Type')}")
            elif status not in (200, 404):
                problems.append(f"{path} 返回了意外状态 {status}")

        status, _, body = _get(port, "/report.json")
        if status == 200 and expected is not None:
            try:
                json.loads(body)
            except json.JSONDecodeError:
                problems.append("/report.json 返回的不是合法 JSON")

        for path in ("/nope", "/../README.md", "/%2e%2e/README.md",
                     "/clashhunter/sources.py", "/./dist/clash.yaml"):
            status, _, _ = _get(port, path)
            if status != 404:
                problems.append(f"白名单之外的路径 {path} 必须 404，实得 {status}")

        status, _, body = _get(port, "/healthz")
        if status != 200 or b"ok" not in body:
            problems.append(f"/healthz 应返回 200 + ok，实得 {status}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 订阅站点通过：白名单只放行产物文件、遍历路径全部 404、"
          "订阅逐字节一致、no-store 且端口合法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
