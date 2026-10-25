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
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def source_urls(limit: int = 24) -> list[str]:
    """每个源站**真正会被下载的那个地址**。

    以前是从 `SOURCES` 块里正则抠 `"https://..."`。那个做法对 `github-raw` 是错的：
    它抠到的是目录（`.../main/`），而 GitHub raw 对目录回 404 —— 于是自检里
    zhuhaiuk / TG-NAV 永远是 FAIL，新加的 cn-news / sunmiao 也一样。
    **自检探的地址和流水线真正下载的地址不是同一个**，那份 FAIL 就毫无信息量，
    还会让真正的故障淹没在里面。

    所以现在直接 import SOURCES，按 kind 拼出实际地址：
      github-raw  -> base + extra[0]（真正的文件名）
      direct      -> extra 里的接口
      gh-uploads  -> trees API（它没有固定文件名，靠 API 列目录）
      其余        -> 索引页/清单页本身
    导入失败（比如没装 PyYAML）就退回正则，自检不能因为这个整块挂掉。

    ⚠️ `limit` 必须大于等于源站数。它原来是 12，而源站从 10 涨到 14 之后，
       **后加的源直接被截掉**，自检里看不见 —— 而这份自检存在的唯一理由就是
       「流水线红了但读不到日志时，仓库里还有证据」。
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from clashhunter.sources import SOURCES

        out: list[str] = []
        for source in SOURCES:
            if source.kind == "github-raw":
                urls = ([source.url.rstrip("/") + "/" + source.extra[0]]
                        if source.extra else [])
            elif source.kind == "direct":
                urls = list(source.extra)
            elif source.kind == "gh-uploads":
                urls = [f"https://api.github.com/repos/{source.url}/git/trees/HEAD"]
            else:
                # manifest / index / wp-dated / dated-sub：探索引页就够了。
                # dated-sub 的带日期文件名只能从文章正文里拿到，索引页能通
                # 就说明「文章还能抓」，这已经是自检能给的最强结论。
                urls = [source.url]
            for url in urls:
                if url not in out:
                    out.append(url)
        if out:
            return out[:limit]
    except Exception:  # noqa: BLE001 - 退化为正则
        pass

    path = ROOT / "clashhunter" / "sources.py"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    start = text.find("SOURCES")
    block = text[start:] if start >= 0 else text
    out = []
    for url in re.findall(r'"(https://[^"]+)"', block):
        if "{" in url:
            continue
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


# 管道自检：6 MB 落在 Cloudflare 的「11~20 MB 稳定 403」区间之外，
# 6 秒墙钟封顶，所以单连接 + 6 并发两轮最多 ~12 秒。
BW_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
BW_BYTES = 6_000_000
BW_TIMEOUT = 6.0
BW_CONNS = 6

# ⚠️ 下面三个数必须与 `clashhunter/builder.py` 的同名常量一致 —— 这里不能 import
# （本脚本跑在 pip install 之前，只能用标准库）。`tests/test_pipe.py` 有断言盯着
# 这三个数，改一处而漏另一处会被测出来。
PIPE_MIN_MBS = 20.0         # ↔ builder.SOURCE_MIN_MBS
PIPE_RESOLVE_MBS = 0.5      # ↔ builder.SCREEN_RESOLVE_MBS（每连接至少分到多少）
PIPE_SCREEN_MB = 0.25       # ↔ builder.SCREEN_BYTES（窄管道下每节点下载量）


def _pull(url: str, *, max_bytes: int, timeout: float) -> dict:
    """读一段数据，返回 {bytes, first, last, err}。时刻用 monotonic（跨线程可比）。"""
    if "{bytes}" in url:
        url = url.replace("{bytes}", str(max_bytes))
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "*/*", "Connection": "close"})
    started = time.monotonic()
    total = 0
    first = last = 0.0
    err = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while total < max_bytes:
                chunk = resp.read(min(65536, max_bytes - total))
                if not chunk:
                    break
                total += len(chunk)
                last = time.monotonic()
                if not first:
                    first = last
                if last - started > timeout:
                    break
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    return {"bytes": total, "first": first, "last": last, "err": err}


def _aggregate(url: str, *, conns: int, max_bytes: int, timeout: float) -> dict:
    with ThreadPoolExecutor(max_workers=max(1, conns)) as pool:
        rows = list(pool.map(
            lambda _i: _pull(url, max_bytes=max_bytes, timeout=timeout),
            range(max(1, conns))))
    got = [r for r in rows if r["bytes"] > 0 and r["first"]]
    total = sum(r["bytes"] for r in got)
    first = min((r["first"] for r in got), default=0.0)
    last = max((r["last"] for r in got), default=0.0)
    span = max(last - first, 0.05) if first else 0.0
    return {"conns": conns, "ok": len(got), "bytes": total,
            "mbps": round(total / 1048576 / span, 2) if span else 0.0,
            "errs": sorted({r["err"] for r in rows if r["err"]})}


def bandwidth() -> dict:
    """单连接 vs 并发总量 —— 分开量「按连接限速」和「出口真窄」。

    为什么必须分开：单连接 1 MB/s 有两种完全相反的解释 ——
      ① 出口总带宽就是 1 MB/s ⇒ 32 并发 × 6 MB 必然整片超时；
      ② 只是每条连接被限到 1 MB/s ⇒ 并发能拿到接近 K 倍，32 并发完全跑得动。
    两者的修法相反，所以一个数不能当另一个用。

    ⚠️ 2026-10-24 单连接 1.06 MB/s 而日志是「下载测速可用 0/354」，
       这两件事**互相解释不了**：若管道真是 1 MB/s 被 32 并发均分，
       每个节点 10 秒还能读到约 300 KB、算出 0.03 MB/s，会被计为「可用」。
       0 只出现在「一个字节都没读到」或「不到 64 KB」。所以那个 0 至今原因未定，
       探因靠 mihomo.measure_speed_ex() 带出来的失败原因。
    """
    single = _aggregate(BW_URL, conns=1, max_bytes=BW_BYTES, timeout=BW_TIMEOUT)
    many = _aggregate(BW_URL, conns=BW_CONNS, max_bytes=BW_BYTES, timeout=BW_TIMEOUT)
    ratio = round(many["mbps"] / single["mbps"], 2) if single["mbps"] > 0 else None
    return {"url": BW_URL, "bytes": BW_BYTES, "timeout": BW_TIMEOUT,
            "single": single, "many": many, "ratio": ratio,
            "verdict": _verdict(single["mbps"], many["mbps"], ratio)}


def _verdict(single: float, agg: float, ratio: float | None) -> str:
    """一句话结论。**结论必须和 `builder._fit_to_pipe` 是同一套规则**，
    否则这里说「压到 N 并发」而流水线里压成另一个数 —— 报告和代码各说各话。

    ⚠️ 这段话曾经写死成「单并发 × 1 MB」，而代码早已改成「N 并发 × 256 KB」。
       数字对不上不只是文案问题：单并发那一版实测把 964 秒里的 480 秒花在等
       50 个卡住的节点上（超时是延迟不是带宽，串行就是把它们一个个等过去）。
       文案跟着规则走，规则变了文案必须一起改 —— `tests/test_pipe.py` 现在
       既比对常量、也禁止这里的过时措辞。
    """
    if agg <= 0:
        return "并发自检没读到数据 —— 管道宽度未知，测速环节只能沿用默认参数"
    if ratio and ratio > 1.5:
        note = f"单连接被限速（并发/单连 {ratio}×），管道按并发总量算；"
    else:
        note = f"并发没有提升（{ratio}×），出口总带宽就是这么多；"
    if agg < PIPE_MIN_MBS:
        # 与 builder._fit_to_pipe 的窄管道分支逐字对应：并发 = 管道 / 0.5，
        # 下限 2、上限 --speed-workers（默认 32）；每节点 256 KB。
        conns = max(2, min(32, int(agg / PIPE_RESOLVE_MBS)))
        return (f"{note}管道 ≈ {agg:.2f} MB/s < 门槛 {PIPE_MIN_MBS:.0f} "
                f"⇒ 初筛按「每连接 {PIPE_RESOLVE_MBS} MB/s」开 {conns} 并发 × "
                f"{PIPE_SCREEN_MB * 1024:.0f} KB，每连接读数天花板约 "
                f"{agg / conns:.2f} MB/s（再快的节点靠 1 并发复测量真值）")
    return (f"{note}管道 ≈ {agg:.2f} MB/s ≥ 门槛 {PIPE_MIN_MBS:.0f} "
            f"⇒ 初筛不压并发，只按「超时的一半」收")


def purity_checks() -> dict:
    """纯净度那两路接口能不能从这台机器打到。

    为什么值得单独探：这两路都**不影响出产物**（拿不到就退回「未知」），
    所以它们坏掉时整条流水线照样绿 —— 唯一能发现的地方就是这里。
    判据：`ip_api` 有 `proxy/hosting/mobile` 字段，`scamalytics` 页面上有
    `Fraud Score`。少一个字段就等于那一路白跑。
    """
    out: dict = {"ip_api": {}, "scamalytics": {}}
    # 拿一个已知的机房 IP 当对照样本：它必须回 hosting=true。
    # 没有对照的话，「查不到」和「查到了但字段没返回」在日志里长得一样。
    try:
        body = _text(
            "http://ip-api.com/json/104.16.132.229"
            "?fields=status,countryCode,proxy,hosting,mobile,isp"
        )
        data = json.loads(body)
        out["ip_api"] = {
            "ok": data.get("status") == "success",
            "hosting": data.get("hosting"),
            "proxy": data.get("proxy"),
            "mobile": data.get("mobile"),
            "has_flags": all(k in data for k in ("proxy", "hosting", "mobile")),
        }
    except Exception as exc:  # noqa: BLE001
        out["ip_api"] = {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
    try:
        row = probe("https://scamalytics.com/ip/104.16.132.229", capture=200000)
        body = row.pop("body", "") or ""
        match = re.search(r"Fraud Score:\s*(\d{1,3})", body)
        out["scamalytics"] = {
            "ok": bool(match),
            "fraud": int(match.group(1)) if match else None,
            **row,
        }
    except Exception as exc:  # noqa: BLE001
        out["scamalytics"] = {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
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
        "bandwidth": bandwidth(),
        "purity": purity_checks(),
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
    bw = report["bandwidth"]
    one, many = bw["single"], bw["many"]
    print(f"\n== 出口管道（{bw['url']}，每次 {bw['bytes'] / 1e6:.0f} MB）")
    print(f"  单连接  {one['mbps']:6.2f} MB/s  {one['ok']}/1 条有数据"
          + (f"  {one['errs'][0]}" if one["errs"] else ""))
    print(f"  {many['conns']} 并发 {many['mbps']:6.2f} MB/s  {many['ok']}/{many['conns']} 条有数据"
          + (f"  {many['errs'][0]}" if many["errs"] else ""))
    print(f"  ⇒ {bw['verdict']}")
    show("采集源站", report["sources"])
    pur = report["purity"]
    print("\n== 纯净度接口（拿不到只是「未知」，不会让流水线红）")
    print(f"  ip-api      {'OK' if pur['ip_api'].get('ok') else 'FAIL'}  "
          f"flags={'齐' if pur['ip_api'].get('has_flags') else '缺'}  "
          f"hosting={pur['ip_api'].get('hosting')} proxy={pur['ip_api'].get('proxy')}"
          + (f"  {pur['ip_api'].get('err')}" if pur['ip_api'].get("err") else ""))
    print(f"  Scamalytics {'OK' if pur['scamalytics'].get('ok') else 'FAIL'}  "
          f"fraud={pur['scamalytics'].get('fraud')}  "
          f"{pur['scamalytics'].get('status') or '-'} "
          f"{pur['scamalytics'].get('ms') or 0}ms"
          + (f"  {pur['scamalytics'].get('err')}" if pur['scamalytics'].get("err") else ""))
    print(f"\n写入 {out}（{report['elapsed_s']}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
