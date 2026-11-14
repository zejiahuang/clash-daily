#!/usr/bin/env python3
"""把 `dist/` 里**测速之后**的订阅产物挂到 HTTP 上（默认 `0.0.0.0:60066`）。

只做三件事：
  /                    一个自包含的静态页（订阅地址、本轮统计、源站状态）
  /clash.yaml          订阅本体，`text/plain`
  /clash.base64.txt    base64 版，给只认 base64 的客户端

同时兼容 `/dist/<文件名>` 这种写法（有人会把路径直接拼成仓库里的位置）。
文件每次请求都从磁盘重读，所以每日流水线更新 `dist/` 之后**不用重启服务**。

为什么用标准库：这台机器上只保证有 Python，装 Flask 只会多一个凌晨三点可能
装不上的依赖。产物只有几个文件、每天几十次请求，`http.server` 足够了。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
# ⚠️ 端口上限是 65535：之前有人想用 66666，它**绑不上**（OverflowError）。
#    60066 是合法值，留在这里也方便一眼看出「不是随手写的」。
DEFAULT_PORT = 60066

# 白名单：只允许这几个文件，其它路径一律 404。绝不按 URL 拼路径 ——
# 那样 `/%2e%2e/README.md` 之类的请求会把仓库里的任意文件读出去。
FILES = {
    "/clash.yaml": ("clash.yaml", "text/plain; charset=utf-8"),
    "/clash.base64.txt": ("clash.base64.txt", "text/plain; charset=utf-8"),
    "/report.json": ("report.json", "application/json; charset=utf-8"),
    "/report.md": ("report.md", "text/plain; charset=utf-8"),
}

_CSS = """
:root{--bg:#0f1420;--card:#171e2e;--line:#26314a;--fg:#e8edf7;--muted:#93a1bd;
--accent:#4f8cff;--ok:#3ecf8e;--warn:#f5a524;--bad:#f45b69}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:40px 20px 64px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:6px}
h1{font-size:26px;margin:0;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
background:rgba(62,207,142,.12);color:var(--ok);font-size:12px;border:1px solid rgba(62,207,142,.3)}
.pill.off{background:rgba(244,91,105,.12);color:var(--bad);border-color:rgba(244,91,105,.3)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px;margin-top:18px}
.card h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;
letter-spacing:.4px;text-transform:uppercase}
.row{display:flex;gap:10px;align-items:center;margin-top:10px}
.row:first-of-type{margin-top:0}
.row label{flex:0 0 132px;color:var(--muted);font-size:13px}
.row input{flex:1;min-width:0;background:#0b1120;color:var(--fg);border:1px solid var(--line);
border-radius:9px;padding:9px 12px;font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
button{flex:0 0 auto;background:var(--accent);color:#fff;border:0;border-radius:9px;
padding:9px 14px;font-size:13px;cursor:pointer}
button:hover{filter:brightness(1.1)}
button.done{background:var(--ok)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.stat{background:#0b1120;border:1px solid var(--line);border-radius:11px;padding:10px 12px}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:12px}
.bar{display:grid;grid-template-columns:84px 1fr 38px;gap:10px;align-items:center;
margin-top:8px;font-size:13px}
.bar .track{height:7px;background:#0b1120;border-radius:99px;overflow:hidden}
.bar .fill{height:100%;background:linear-gradient(90deg,#4f8cff,#7c5cff);border-radius:99px}
.bar em{font-style:normal;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.err{color:var(--bad)}.note{color:var(--warn)}
footer{margin-top:26px;color:var(--muted);font-size:12px;line-height:1.8}
footer a{color:var(--accent);text-decoration:none}
.tag{display:inline-block;padding:1px 7px;border-radius:6px;background:rgba(79,140,255,.12);
color:var(--accent);font-size:11px;margin-left:6px}
@media(max-width:620px){.row{flex-wrap:wrap}.row label{flex-basis:100%}}
"""

_COPY_JS = """
function copyText(id, btn) {
  var el = document.getElementById(id);
  var text = el.value || el.textContent;
  var done = function () { btn.textContent = '已复制'; btn.classList.add('done');
    setTimeout(function () { btn.textContent = '复制'; btn.classList.remove('done'); }, 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, function () { el.select(); document.execCommand('copy'); done(); });
  } else { el.select(); document.execCommand('copy'); done(); }
}
"""


def _load_report(dist: Path) -> dict:
    try:
        data = json.loads((dist / "report.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 页面没有报告也要能打开
        return {}


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _stat_card(value, label: str) -> str:
    return f'<div class="stat"><b>{value}</b><span>{label}</span></div>'


def _country_bars(countries: dict) -> str:
    if not countries:
        return '<p class="sub">报告里没有国家分布。</p>'
    items = sorted(countries.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:10]
    top = max(int(v) for _, v in items) or 1
    rows = []
    for name, count in items:
        width = max(3, round(int(count) * 100 / top))
        rows.append(
            f'<div class="bar"><span>{html.escape(str(name))}</span>'
            f'<span class="track"><span class="fill" style="width:{width}%"></span></span>'
            f'<em>{_fmt_int(count)}</em></div>'
        )
    return "".join(rows)


def _source_rows(sources: list) -> str:
    if not sources:
        return '<tr><td colspan="3" class="sub">报告里没有源站信息。</td></tr>'
    rows = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        state = ""
        if item.get("error"):
            state = f'<span class="err">失败：{html.escape(str(item["error"]))}</span>'
        elif item.get("note"):
            state = f'<span class="note">{html.escape(str(item["note"]))}</span>'
        else:
            state = '<span style="color:var(--ok)">OK</span>'
        rows.append(
            f'<tr><td><code>{html.escape(str(item.get("id", "")))}</code></td>'
            f'<td class="num">{_fmt_int(item.get("count"))}</td><td>{state}</td></tr>'
        )
    return "".join(rows)


def render_page(report: dict, base: str, dist: Path) -> str:
    """生成自包含的订阅页。`base` 是形如 `http://1.2.3.4:66666` 的前缀。"""
    yaml_url = base + "/clash.yaml"
    b64_url = base + "/clash.base64.txt"

    stats = report.get("stats") or {}
    nodes = report.get("nodes") or []
    generated = str(report.get("generated_at") or "").strip()
    accepted = stats.get("accepted", len(nodes) if nodes else 0)
    ready = bool(nodes) and (dist / "clash.yaml").is_file()

    if ready:
        badge = f'<span class="pill">● 在线 · {_fmt_int(accepted)} 个节点</span>'
    else:
        badge = '<span class="pill off">● 还没有可用产物</span>'

    generated_line = (
        f"最近一轮：{html.escape(generated)}" if generated
        else "还没有跑过一轮（或 report.json 缺失）"
    )
    vantage = str(report.get("vantage") or "").strip()
    vantage_line = f"<br>测速位置：{html.escape(vantage)}" if vantage else ""

    cards = "".join([
        _stat_card(_fmt_int(accepted), "最终节点"),
        _stat_card(_fmt_int(stats.get("raw")), "原始"),
        _stat_card(_fmt_int(stats.get("unique")), "去重后"),
        _stat_card(_fmt_int(stats.get("tcp_alive")), "TCP 通过"),
        _stat_card(_fmt_int(stats.get("delay_ok")), "延迟可用"),
        _stat_card(_fmt_int(stats.get("speed_ok")), "测速可用"),
    ])

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>clash-daily 订阅</title>
<style>{_CSS}</style>
</head><body><div class="wrap">
<header>
  <h1>clash-daily</h1>{badge}
</header>
<div class="sub">{generated_line}{vantage_line}</div>

<section class="card">
  <h2>订阅地址<span class="tag">每天自动更新</span></h2>
  <div class="row">
    <label>Clash / Mihomo</label>
    <input id="url-yaml" readonly value="{html.escape(yaml_url)}">
    <button onclick="copyText('url-yaml', this)">复制</button>
  </div>
  <div class="row">
    <label>Base64</label>
    <input id="url-b64" readonly value="{html.escape(b64_url)}">
    <button onclick="copyText('url-b64', this)">复制</button>
  </div>
  <p class="sub">把上面的地址直接粘进 Clash Verge / Mihomo / ClashX Meta 的「订阅」即可。
  只认 base64 的客户端用第二条。速度是「测速机 → 节点」的实测值，不代表你所在网络。</p>
</section>

<section class="card">
  <h2>本轮统计</h2>
  <div class="grid">{cards}</div>
</section>

<section class="card">
  <h2>出口地区</h2>
  {_country_bars(report.get("countries") or {})}
</section>

<section class="card">
  <h2>源站状态</h2>
  <table>
    <thead><tr><th>源站</th><th class="num">节点</th><th>状态</th></tr></thead>
    <tbody>{_source_rows(report.get("sources") or [])}</tbody>
  </table>
</section>

<footer>
  产物文件：<a href="/clash.yaml">clash.yaml</a> ·
  <a href="/clash.base64.txt">clash.base64.txt</a> ·
  <a href="/report.md">report.md</a> ·
  <a href="/report.json">report.json</a><br>
  本页与订阅文件都从 <code>dist/</code> 实时读取，每日流水线跑完即生效，无需重启。
</footer>
</div>
<script>{_COPY_JS}</script>
</body></html>"""


class DistHandler(BaseHTTPRequestHandler):
    dist: Path = ROOT / "dist"
    server_version = "clash-daily-serve/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    # ---------------------------------------------------------------- 内部

    def _base_url(self) -> str:
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (self.headers.get("Host") or "").strip()
        if not host:
            address = getattr(self.server, "server_address", ("127.0.0.1", DEFAULT_PORT))
            host = f"{address[0]}:{address[1]}"
        return f"{proto}://{host}"

    def _send(self, status, ctype: str, body: bytes, head_only: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 产物每天都会变，任何一层缓存都可能让人读到昨天的节点。
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve(self, *, head_only: bool) -> None:
        path = unquote(urlsplit(self.path).path or "/")

        if path in ("/", "/index.html"):
            page = render_page(_load_report(self.dist), self._base_url(), self.dist)
            self._send(HTTPStatus.OK, "text/html; charset=utf-8",
                       page.encode("utf-8"), head_only)
            return

        if path == "/healthz":
            self._send(HTTPStatus.OK, "application/json; charset=utf-8",
                       b'{"ok":true}', head_only)
            return

        if path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, "image/x-icon", b"", head_only)
            return

        # /dist/clash.yaml 与 /clash.yaml 等价
        if path.startswith("/dist/"):
            path = "/" + path[len("/dist/"):]

        item = FILES.get(path)
        if not item:
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8",
                       "404 Not Found\n".encode(), head_only)
            return
        name, ctype = item
        target = self.dist / name
        if not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8",
                       f"还没有产物：{name}\n".encode(), head_only)
            return
        try:
            body = target.read_bytes()
        except OSError as exc:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain; charset=utf-8",
                       f"读取失败：{exc}\n".encode(), head_only)
            return
        self._send(HTTPStatus.OK, ctype, body, head_only)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{stamp}] {self.address_string()} {fmt % args}\n")


def make_handler(dist: Path):
    class _Bound(DistHandler):
        pass

    _Bound.dist = Path(dist)
    return _Bound


def make_server(bind: str, port: int, dist: Path) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), make_handler(dist))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 dist/ 的订阅产物挂到 HTTP 上")
    parser.add_argument("--bind", default=os.environ.get("BIND", "0.0.0.0"),
                        help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--dist", default=os.environ.get("DIST", str(ROOT / "dist")),
                        help="产物目录（默认仓库下的 dist/）")
    args = parser.parse_args(argv)

    dist = Path(args.dist).expanduser().resolve()
    server = make_server(args.bind, args.port, dist)
    print(f"[serve] http://{args.bind}:{args.port}  产物目录 {dist}", flush=True)
    if not (dist / "clash.yaml").is_file():
        print(f"[serve] [warn] {dist / 'clash.yaml'} 还不存在，页面会显示「没有可用产物」",
              flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
