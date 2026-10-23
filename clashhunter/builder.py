"""串起整条流水线：采集 -> 去重 -> TCP 预筛 -> 真实测速 -> 出口国家 -> 重命名 -> 出订阅。"""

from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import countries as country_mod
from . import geoip
from . import netcheck
from . import nodes as nodemod
from . import sources as source_mod
from .mihomo import GROUP, TEST_URL, Mihomo, ensure_binary

CST = timezone(timedelta(hours=8))


@dataclass
class Stats:
    raw: int = 0
    unique: int = 0
    duplicates: int = 0
    tcp_alive: int = 0
    loadable: int = 0
    rejected: int = 0
    alive: int = 0
    accepted: int = 0


@dataclass
class BuildResult:
    proxies: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    sources: list[dict] = field(default_factory=list)
    countries: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""


# --------------------------------------------------------------------------- 测速


def _delay_all(mh: Mihomo, proxies: list[dict], *, url: str, timeout_ms: int,
               workers: int, log=print) -> dict[str, int]:
    results: dict[str, int] = {}
    total = len(proxies)
    if not total:
        return results
    log(f"  真实测速：{total} 个节点，并发 {workers}，单节点超时 {timeout_ms}ms")

    def job(proxy: dict):
        return proxy["name"], mh.delay(proxy["name"], url, timeout_ms)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, p) for p in proxies]
        for future in as_completed(futures):
            try:
                name, value = future.result()
            except Exception:  # noqa: BLE001
                continue
            if value:
                results[name] = value
            done += 1
            if done % 100 == 0 or done == total:
                log(f"    ... {done}/{total}，可用 {len(results)}")
    return results


def _probe_exit_ips(mh: Mihomo, proxies: list[dict], *, timeout: float,
                    log=print) -> dict[str, str]:
    """逐个切到目标节点，从混合端口发请求问"我是谁"。必须串行——选择器是全局状态。"""
    out: dict[str, str] = {}
    total = len(proxies)
    log(f"  出口 IP 探测：{total} 个节点（串行，选择器是全局状态）")
    for index, proxy in enumerate(proxies, 1):
        if not mh.select(proxy["name"]):
            continue
        ip = mh.http_via_proxy("http://api.ipify.org", timeout=timeout)
        if not ip or not _is_ip(ip):
            ip = mh.http_via_proxy("http://icanhazip.com", timeout=timeout)
        if ip and _is_ip(ip):
            out[proxy["name"]] = ip
        if index % 25 == 0 or index == total:
            log(f"    ... {index}/{total}，拿到出口 IP {len(out)}")
    return out


def _is_ip(text: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(text.strip())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- 主流程


def build(
    *,
    out_dir: Path,
    work_dir: Path,
    only: set[str] | None = None,
    max_articles: int = 3,
    tcp_timeout: float = 3.0,
    tcp_workers: int = 128,
    delay_url: str = TEST_URL,
    delay_timeout_ms: int = 5000,
    delay_workers: int = 32,
    max_nodes: int = 300,
    exit_ip_max: int = 300,
    exit_ip_timeout: float = 6.0,
    no_exit_ip: bool = False,
    no_speedtest: bool = False,
    top_per_country: int = 0,
    log=print,
) -> BuildResult:
    started = time.time()
    result = BuildResult()

    # 1. 采集 -------------------------------------------------------------
    log("[1/6] 采集源站")
    reports = source_mod.collect(only=only, max_articles=max_articles, log=log)
    raw: list[dict] = []
    for report in reports:
        raw.extend(report.proxies)
        result.sources.append({
            "id": report.sid,
            "urls": report.urls,
            "articles": report.articles,
            "count": len(report.proxies),
            "error": report.error,
        })
    result.stats.raw = len(raw)
    if not raw:
        raise SystemExit("所有源站都没有解析出节点，检查源站是否改版")

    # 2. 去重 -------------------------------------------------------------
    log("[2/6] 去重")
    unique, dupes = nodemod.dedupe(raw)
    result.stats.unique = len(unique)
    result.stats.duplicates = dupes
    log(f"  原始 {len(raw)}，去重后 {len(unique)}（去掉 {dupes} 个重复）")

    for index, proxy in enumerate(unique, 1):
        proxy["name"] = f"n{index:04d}"

    # 3. TCP 预筛 ---------------------------------------------------------
    log("[3/6] TCP 预筛")
    endpoint_map = netcheck.probe_endpoints(
        [(p["server"], p["port"]) for p in unique],
        timeout=tcp_timeout, workers=tcp_workers, log=log,
    )
    alive = [p for p in unique if endpoint_map.get((p["server"], p["port"]))]
    result.stats.tcp_alive = len(alive)
    if not alive:
        raise SystemExit("TCP 预筛后一个都不剩，检查运行环境出网是否正常")
    log(f"  通过预筛 {len(alive)}")

    delays: dict[str, int] = {}
    mh: Mihomo | None = None

    if no_speedtest:
        log("[4/6] 跳过真实测速（--no-speedtest）")
        candidates = alive
    else:
        log("[4/6] 加载内核并真实测速")
        binary = ensure_binary(work_dir / "bin", log=log)
        mh = Mihomo(binary, work_dir, log=log)
        try:
            loadable, rejected = mh.filter_loadable(alive)
            result.stats.loadable = len(loadable)
            result.stats.rejected = len(rejected)
            if not loadable:
                raise SystemExit("没有一条能被 mihomo 加载")
            mh.start(loadable)
            delays = _delay_all(
                mh, loadable, url=delay_url, timeout_ms=delay_timeout_ms,
                workers=delay_workers, log=log,
            )
            result.stats.alive = len(delays)
            log(f"  真实可用 {len(delays)}/{len(loadable)}")
            candidates = [p for p in loadable if p["name"] in delays]
            candidates.sort(key=lambda p: delays[p["name"]])

            if not candidates:
                raise SystemExit("测速后没有任何节点可用")

            # 先截断再探测：出口 IP 探测是串行的，不该为最终会丢弃的节点花时间
            if max_nodes > 0:
                candidates = candidates[:max_nodes]

            # 5. 出口国家 --------------------------------------------------
            if no_exit_ip:
                log("[5/6] 跳过出口 IP 探测（--no-exit-ip）")
                exit_ips = {}
            else:
                log("[5/6] 出口 IP 探测")
                probe_list = candidates[:exit_ip_max] if exit_ip_max > 0 else candidates
                exit_ips = _probe_exit_ips(mh, probe_list, timeout=exit_ip_timeout, log=log)
        finally:
            if mh:
                mh.stop()
                mh = None
    if no_speedtest:
        exit_ips = {}

    candidates = candidates[:max_nodes] if max_nodes > 0 else candidates

    # 6. 国家判定 + 重命名 -------------------------------------------------
    log("[6/6] 国家判定与重命名")
    code_of: dict[str, str] = {}

    # 6a. 出口 IP（最准）
    if exit_ips:
        geo = geoip.lookup_batch(list(exit_ips.values()), log=log)
        for name, ip in exit_ips.items():
            hit = geo.get(ip)
            if hit and hit[0]:
                code_of[name] = hit[0]
    log(f"  出口 IP 定位成功 {len(code_of)}")

    # 6b. 服务端 IP 兜底
    rest = [p for p in candidates if p["name"] not in code_of]
    if rest:
        resolved = geoip.resolve_many([p["server"] for p in rest], log=log)
        geo2 = geoip.lookup_batch(list(resolved.values()), log=log)
        for proxy in rest:
            ip = resolved.get(proxy["server"])
            hit = geo2.get(ip) if ip else None
            if hit and hit[0]:
                code_of[proxy["name"]] = hit[0]

    # 6c. 节点名兜底
    for proxy in candidates:
        if proxy["name"] in code_of:
            continue
        hint = country_mod.code_from_name(proxy.get("name") or "")
        if hint:
            code_of[proxy["name"]] = hint
    log(f"  最终定位成功 {sum(1 for p in candidates if p['name'] in code_of)}/{len(candidates)}")

    # 分组重命名
    grouped: dict[str, list[dict]] = defaultdict(list)
    for proxy in candidates:
        grouped[code_of.get(proxy["name"], "??")].append(proxy)

    ordered_codes = sorted(
        grouped,
        key=lambda c: (-len(grouped[c]), country_mod.zh_name(c) or c),
    )

    final: list[dict] = []
    records: list[dict] = []
    counters: dict[str, int] = {}
    for code in ordered_codes:
        label = country_mod.zh_name(code) or (code if code != "??" else country_mod.UNKNOWN_ZH)
        bucket = grouped[code]
        bucket.sort(key=lambda p: delays.get(p["name"], 99999))
        if top_per_country > 0:
            bucket = bucket[:top_per_country]
        for index, proxy in enumerate(bucket, 1):
            original = proxy["name"]
            new_name = f"{label}-{index:02d}"
            proxy = dict(proxy)
            proxy["name"] = new_name
            final.append(proxy)
            counters[label] = counters.get(label, 0) + 1
            records.append({
                "name": new_name,
                "original": original,
                "type": proxy["type"],
                "server": proxy["server"],
                "port": proxy["port"],
                "delay": delays.get(original),
                "exit_ip": exit_ips.get(original),
                "country_code": code,
                "country": label,
            })

    result.proxies = final
    result.records = records
    result.countries = dict(counters)
    result.stats.accepted = len(final)
    result.generated_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %z")
    log(f"  最终 {len(final)} 个节点，覆盖 {len(counters)} 个国家/地区，"
        f"耗时 {time.time() - started:.0f}s")
    return result


# --------------------------------------------------------------------------- 输出


def _header(result: BuildResult) -> str:
    lines = [
        "# ============================================================",
        "# 免费节点聚合订阅 · 自动生成，请勿手动编辑",
        f"# 生成时间：{result.generated_at}",
        f"# 节点数量：{result.stats.accepted}",
        f"# 覆盖地区：{'、'.join(f'{k}×{v}' for k, v in result.countries.items())}",
        "# 节点已按「真实测速延迟」排序，名称格式为「国家-序号」",
        "# 免费节点随时可能失效，仅供学习与技术研究使用",
        "# ============================================================",
        "",
    ]
    return "\n".join(lines)


def render_config(result: BuildResult) -> dict:
    names = [p["name"] for p in result.proxies]
    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": True,
        "external-controller": "127.0.0.1:9090",
        "proxies": result.proxies,
        "proxy-groups": [
            {"name": "节点选择", "type": "select", "proxies": ["自动选择", *names, "DIRECT"]},
            {
                "name": "自动选择",
                "type": "url-test",
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 50,
                "proxies": names,
            },
        ],
        "rules": ["MATCH,节点选择"],
    }


def write_outputs(result: BuildResult, out_dir: Path, *, log=print) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    if not result.proxies:
        log("  [warn] 没有可用节点，跳过订阅文件生成")
        return paths

    body = yaml.safe_dump(
        render_config(result), allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=100000,
    )
    text = _header(result) + body

    clash_path = out_dir / "clash.yaml"
    clash_path.write_text(text, encoding="utf-8")
    paths["clash"] = clash_path

    b64_path = out_dir / "clash.base64.txt"
    b64_path.write_text(base64.b64encode(text.encode("utf-8")).decode("ascii"), encoding="utf-8")
    paths["base64"] = b64_path

    report = {
        "generated_at": result.generated_at,
        "stats": result.stats.__dict__,
        "countries": result.countries,
        "sources": result.sources,
        "nodes": result.records,
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["json"] = json_path

    md_path = out_dir / "report.md"
    md_path.write_text(_render_markdown(result), encoding="utf-8")
    paths["md"] = md_path

    for key, path in paths.items():
        log(f"  写出 {key}: {path} ({path.stat().st_size} B)")
    return paths


def _render_markdown(result: BuildResult) -> str:
    stats = result.stats
    lines = [
        "# 免费节点订阅 · 每日构建报告",
        "",
        f"- 生成时间：**{result.generated_at}**（北京时间）",
        f"- 最终可用节点：**{stats.accepted}**",
        "",
        "## 各阶段过滤",
        "",
        "| 阶段 | 数量 |",
        "| --- | ---: |",
        f"| 原始采集 | {stats.raw} |",
        f"| 去重后 | {stats.unique} |",
        f"| TCP 端口存活 | {stats.tcp_alive} |",
        f"| 内核可加载 | {stats.loadable} |",
        f"| 真实代理可用 | {stats.alive} |",
        f"| 最终收录 | {stats.accepted} |",
        "",
        "## 地区分布",
        "",
        "| 地区 | 节点数 | 最快延迟 |",
        "| --- | ---: | ---: |",
    ]
    fastest: dict[str, int] = {}
    for record in result.records:
        delay = record.get("delay")
        if delay:
            cur = fastest.get(record["country"])
            if cur is None or delay < cur:
                fastest[record["country"]] = delay
    for name, count in result.countries.items():
        ms = fastest.get(name)
        lines.append(f"| {name} | {count} | {f'{ms} ms' if ms else '—'} |")

    lines += ["", "## 延迟最低的 15 个节点", "", "| 名称 | 类型 | 延迟 | 出口 IP |", "| --- | --- | ---: | --- |"]
    ranked = sorted(
        [r for r in result.records if r.get("delay")],
        key=lambda r: r["delay"],
    )[:15]
    for record in ranked:
        lines.append(
            f"| {record['name']} | {record['type']} | {record['delay']} ms | "
            f"{record.get('exit_ip') or '—'} |"
        )

    lines += ["", "## 源站", ""]
    for source in result.sources:
        state = "OK" if not source["error"] else f"失败：{source['error']}"
        lines.append(f"- `{source['id']}`：{source['count']} 个候选 — {state}")
    lines.append("")
    return "\n".join(lines)
