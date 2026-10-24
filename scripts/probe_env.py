#!/usr/bin/env python3
"""CNB runner 环境自检：只记录「哪一次请求发生了什么」，不下结论。

为什么需要它：CNB 的构建日志是前端流式加载的（拉下来的 HTML 里没有日志正文），
仓库令牌也读不到 OpenAPI，所以流水线红了，从外部只能看到「哪个阶段红了」。
这个脚本把每一项检查的结果写成 JSON 提交回仓库 —— 那是唯一读得到的证据。

⚠️ 它跑在 `pip install -r requirements.txt` 之前，所以**只能用标准库**，
   也不能 import clashhunter（那个包 import 时就要 yaml）。源站地址是按正则从
   clashhunter/sources.py 里抠出来的，改 sources.py 时这里会自动跟上。

⚠️ 退出码恒为 0：它是诊断，不是门禁。判据在 JSON 里，不在退出码里。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent

# 与 clashhunter/web.py 的 DEFAULT_MIRRORS 保持一致；这里不能 import，只能抄。
MIRRORS = ("https://gh-proxy.com/", "https://cdn.akaere.online/")

GH_FILE = "https://raw.githubusercontent.com/zhuhaiuk/free-nodes/main/clash_config.yaml"
GH_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=1000000"


# --------------------------------------------------------------------- 单次请求


def probe(url: str, *, limit: int = 65536, timeout: float = 20.0,
          headers: dict | None = None, capture: int = 0) -> dict:
    """发一次 GET（最多读 limit 字节），把结果原样记下来。不抛异常。"""
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if headers:
        hdrs.update(headers)
    row: dict = {"url": url, "ok": False, "status": None, "ms": None,
                 "bytes": 0, "mbps": None, "err": None}
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            row["status"] = resp.status
            chunk = resp.read(limit)
            row["bytes"] = len(chunk)
            if capture:
                row["body"] = chunk[:capture].decode("utf-8", "replace")
        row["ok"] = True
    except urllib.error.HTTPError as exc:
        row["status"] = exc.code
        row["err"] = f"HTTPError {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 诊断脚本，什么异常都要记下来
        row["err"] = f"{type(exc).__name__}: {exc}"
    row["ms"] = round((time.monotonic() - started) * 1000)
    if row["ok"] and row["bytes"] and row["ms"]:
        row["mbps"] = round(row["bytes"] / 1048576 / (row["ms"] / 1000), 2)
    return row


def _text(url: str, *, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ------------------------------------------------------------------- 取被测目标


def source_urls(limit: int = 12) -> list[str]:
    """从 clashhunter/sources.py 的 SOURCES 块里抠出 https 地址。"""
    path = ROOT / "clashhunter" / "sources.py"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    start = text.find("SOURCES")
    block = text[start:] if start >= 0 else text
    out: list[str] = []
    for url in re.findall(r'"(https://[^"]+)"', block):
        if url not in out:
            out.append(url)
    return out[:limit]


def pinned_tag() -> str | None:
    """从 clashhunter/mihomo.py 里抠 PINNED_TAG —— 那是内核下载的最后一条退路。"""
    path = ROOT / "clashhunter" / "mihomo.py"
    if not path.is_file():
        return None
    match = re.search(r'^PINNED_TAG\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8",
                                                                   errors="replace"), re.M)
    return match.group(1) if match else None


EGRESS_URLS = (
    "https://api.ipify.org",
    "http://icanhazip.com",
    "http://ip-api.com/line/?fields=query",
    "https://ifconfig.me/ip",
    "http://ip.3322.net",
)


def egress() -> dict:
    """问「我是谁」。必须多备几个：CNB 上 api.ipify.org 直接 Connection refused
    （实测 2026-10-24），只问它的话这一整块永远是 null，而它恰好是判断
    「runner 到底在哪个国家」的唯一依据。
    """
    out: dict = {"ip": None, "geo": None, "tries": []}
    for url in EGRESS_URLS:
        try:
            ip = _text(url).strip()
            out["tries"].append({"url": url, "ip": ip})
            if re.fullmatch(r"[0-9a-fA-F.:]{7,45}", ip):
                out["ip"] = ip
                break
        except Exception as exc:  # noqa: BLE001
            out["tries"].append({"url": url, "err": f"{type(exc).__name__}: {exc}"})
    if out["ip"]:
        try:
            out["geo"] = json.loads(_text(
                f"http://ip-api.com/json/{out['ip']}"
                "?fields=status,country,countryCode,regionName,city,isp"
            ))
        except Exception as exc:  # noqa: BLE001
            out["err_geo"] = f"{type(exc).__name__}: {exc}"
    return out


# ------------------------------------------------------------------------- 各项


def github_checks() -> dict:
    """GitHub 直连 + 两个镜像前缀，取的是同一个文件。"""
    rows = [probe(GH_FILE)]
    for prefix in MIRRORS:
        rows.append(probe(prefix + GH_FILE))
    return {"file": GH_FILE, "rows": rows}


def kernel_checks() -> dict:
    api = probe(GH_API, capture=4000)
    tag = None
    if api.get("body"):
        match = re.search(r'"tag_name"\s*:\s*"([^"]+)"', api["body"])
        tag = match.group(1) if match else None
    out: dict = {"api": api, "tag_from_api": tag, "tag_pinned": pinned_tag(),
                 "tag_used": None, "rows": []}
    out["tag_used"] = tag or out["tag_pinned"]
    if not out["tag_used"]:
        return out
    name = f"mihomo-linux-amd64-compatible-{out['tag_used']}.gz"
    base = f"https://github.com/MetaCubeX/mihomo/releases/download/{out['tag_used']}/{name}"
    # 只取前 1 MB：要判断的是「这条路径能不能下载」，不是把 40 MB 拉一遍
    hdrs = {"Range": "bytes=0-1048575"}
    out["rows"].append(probe(base, limit=1048576, headers=hdrs))
    for prefix in MIRRORS:
        out["rows"].append(probe(prefix + base, limit=1048576, headers=hdrs))
    return out


def _probe_source(url: str) -> dict:
    """探一个采集源站。

    ⚠️ 只跳过一种：`raw.githubusercontent.com/<owner>/<repo>/<分支>/` 这种「目录 +
       固定文件名」的源（`github-raw`）。目录本身不是可下载的资源，请求它必然 404，
       而 404 会被读成「源站挂了」—— 它只是「这个地址本来就不该被请求」。
       GitHub 主机的连通性由 github_checks() 用一个真实文件测。
       ⚠️ 别把规则写成「以 / 结尾就跳过」：CMS 的索引页（`clash-node.com/free-node/`）
          正是最需要测的那些。
    """
    host = urlsplit(url).hostname or ""
    if url.endswith("/") and host == "raw.githubusercontent.com":
        return {"url": url, "ok": None, "status": None, "ms": None, "bytes": 0,
                "mbps": None, "err": None,
                "skipped": "github-raw 目录地址，不是文件；主机连通性见 github 段"}
    return probe(url, limit=2048)


def system_info() -> dict:
    """容器本身的限制。为什么要看这个：354 个节点全部「可用 0」时，
    除了「带宽不够」还有一种解释是 mihomo 根本没起来（FD 用完 / 内存被杀），
    两者的修法完全不同，而这两个数一眼就能分开。
    """
    out: dict = {}
    try:
        out["nproc"] = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        out["nproc"] = os.cpu_count()
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        out["nofile"] = [soft, hard]
    except Exception as exc:  # noqa: BLE001
        out["nofile"] = f"{type(exc).__name__}: {exc}"
    for key, path in (("mem_total_kb", "/proc/meminfo"),
                      ("cgroup_mem_max", "/sys/fs/cgroup/memory.max"),
                      ("cgroup_mem_max_v1", "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            if key == "mem_total_kb":
                match = re.search(r"MemTotal:\s+(\d+) kB", text)
                out[key] = int(match.group(1)) if match else None
            else:
                out[key] = text.strip()
        except Exception:  # noqa: BLE001
            out[key] = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="CNB runner 环境自检")
    ap.add_argument("--out", default="dist/probe.json")
    args = ap.parse_args()

    started = time.monotonic()
    report: dict = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "system": system_info(),
        "egress": egress(),
        "github": github_checks(),
        "kernel": kernel_checks(),
        "speed_source": [probe(SPEED_URL, limit=1048576)],
        "sources": [_probe_source(u) for u in source_urls()],
    }
    report["elapsed_s"] = round(time.monotonic() - started, 1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def show(title: str, rows: list[dict]) -> None:
        print(f"\n== {title}")
        for row in rows:
            if row.get("skipped"):
                print(f"  SKIP        {row['url']}\n            {row['skipped']}")
                continue
            mark = "OK  " if row["ok"] else "FAIL"
            extra = f"{row['mbps']:.2f} MB/s" if row["mbps"] else (row["err"] or "")
            print(f"  {mark} {row['status'] or '-':>4} {row['ms'] or 0:>6}ms  "
                  f"{row['url']}\n            {extra}")

    sysinfo = report["system"]
    print(f"容器：{sysinfo.get('nproc')} 核，"
          f"nofile={sysinfo.get('nofile')}，"
          f"内存上限={sysinfo.get('cgroup_mem_max') or sysinfo.get('cgroup_mem_max_v1')}"
          f"（MemTotal {sysinfo.get('mem_total_kb')} kB）")
    geo = report["egress"].get("geo") or {}
    print(f"出口 IP: {report['egress']['ip']}  "
          f"{geo.get('country') or ''}{geo.get('city') or ''} {geo.get('isp') or ''}")
    show("GitHub raw（直连 + 镜像）", report["github"]["rows"])
    print(f"\n== 内核 tag：api={report['kernel']['tag_from_api']} "
          f"pinned={report['kernel']['tag_pinned']} used={report['kernel']['tag_used']}")
    show("内核下载（Range 0-1MB）", report["kernel"]["rows"])
    show("测速源", report["speed_source"])
    show("采集源站", report["sources"])
    print(f"\n写入 {out}（{report['elapsed_s']}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
