"""串起整条流水线：采集 -> 去重 -> TCP 预筛 -> 延迟测速 -> 下载测速+出口国家 -> 重命名 -> 出订阅。"""

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
from .mihomo import SPEED_URL, TEST_URL, Mihomo, ensure_binary

CST = timezone(timedelta(hours=8))


@dataclass
class Stats:
    raw: int = 0
    unique: int = 0
    duplicates: int = 0
    tcp_alive: int = 0
    loadable: int = 0
    rejected: int = 0
    delay_ok: int = 0
    speed_ok: int = 0
    accepted: int = 0


@dataclass
class BuildResult:
    proxies: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    sources: list[dict] = field(default_factory=list)
    countries: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""


# --------------------------------------------------------------------------- 延迟测速


def _delay_all(mh: Mihomo, proxies: list[dict], *, url: str, timeout_ms: int,
               workers: int, log=print) -> dict[str, int]:
    results: dict[str, int] = {}
    total = len(proxies)
    if not total:
        return results
    log(f"  延迟测速：{total} 个节点，并发 {workers}，单节点超时 {timeout_ms}ms")

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
    speed_url: str = SPEED_URL,
    speed_bytes: int = 2_000_000,
    speed_timeout: float = 10.0,
    speed_workers: int = 32,
    speed_max: int = 0,
    refine_top: int = 300,
    refine_workers: int = 16,
    ip_timeout: float = 6.0,
    max_nodes: int = 100,
    top_per_country: int = 0,
    no_speedtest: bool = False,
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
            "note": report.note,
        })
    result.stats.raw = len(raw)
    failed = [s["id"] for s in result.sources if s["error"]]
    if failed:
        log(f"  [warn] 以下源站失败: {', '.join(failed)}")
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
    probes: dict[str, dict] = {}
    pre_codes: dict[str, str] = {}
    candidates = alive

    if no_speedtest:
        log("[4/6] 跳过测速（--no-speedtest）")
    else:
        log("[4/6] 延迟测速")
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
            result.stats.delay_ok = len(delays)
            log(f"  延迟可用 {len(delays)}/{len(loadable)}")
            candidates = [p for p in loadable if p["name"] in delays]
            candidates.sort(key=lambda p: delays[p["name"]])
            if not candidates:
                raise SystemExit("延迟测速后没有任何节点可用")

            # 延迟排序只用于「谁先测」和兜底，不再用来砍数量。
            # 曾经的做法是测速前按国家分层砍到 speed_max 个，理由是省时间；
            # 但那样「没被抽中测速」的节点永远进不了最终名单，等于用延迟预先决定了结果。
            # 现在默认全量测速（speed_max=0），只在显式指定上限时才截断。
            pre_codes = _server_countries(candidates, log=log)
            if 0 < speed_max < len(candidates):
                candidates = _stratified(
                    candidates, speed_max, pre_codes, key=lambda p: delays[p["name"]]
                )
                log(f"  按国家分层截断后进入测速 {len(candidates)} 个，"
                    f"覆盖 {len({pre_codes.get(p['name'], '??') for p in candidates})} 个地区"
                    f"（speed_max={speed_max}）")
            else:
                log(f"  全量测速 {len(candidates)} 个，覆盖 "
                    f"{len({pre_codes.get(p['name'], '??') for p in candidates})} 个地区"
                    f"（不预采样）")

            # 5. 下载测速 + 出口 IP --------------------------------------
            log("[5/6] 下载测速与出口 IP")
            probes = mh.probe_all(
                candidates,
                speed_url=speed_url, speed_bytes=speed_bytes,
                speed_timeout=speed_timeout, ip_timeout=ip_timeout,
                workers=speed_workers, log=log,
            )
            result.stats.speed_ok = sum(1 for v in probes.values() if v.get("speed", 0) > 0)
            log(f"  下载测速可用 {result.stats.speed_ok}/{len(candidates)}")

            # 复测：高并发初筛时几十个节点在抢同一条出口带宽，快节点会被压低，
            # 而写进节点名的就是这个数。所以把「可能进入最终名单」的那批用低并发重测一次。
            # 只覆盖复测确实测出速度的项 —— 复测失败不能把初筛的有效结果冲掉。
            if refine_top > 0:
                ranked = sorted(candidates, key=lambda p: -_speed_of(p, probes))
                refine = [p for p in ranked[:refine_top] if _speed_of(p, probes) > 0]
                if refine:
                    log(f"  复测前 {len(refine)} 个（并发 {refine_workers}，"
                        f"消除初筛抢带宽造成的低估）")
                    again = mh.probe_all(
                        refine,
                        speed_url=speed_url, speed_bytes=speed_bytes,
                        speed_timeout=speed_timeout, ip_timeout=ip_timeout,
                        workers=refine_workers, log=log,
                    )
                    improved = 0
                    for name, item in again.items():
                        if item.get("speed", 0) > 0:
                            if item["speed"] > probes.get(name, {}).get("speed", 0):
                                improved += 1
                            probes[name] = item
                    result.stats.speed_ok = sum(
                        1 for v in probes.values() if v.get("speed", 0) > 0)
                    log(f"  复测完成，其中 {improved} 个比初筛更快；"
                        f"总计可用 {result.stats.speed_ok}")
        finally:
            mh.stop()

    # 6. 国家判定 + 重命名 -------------------------------------------------
    log("[6/6] 国家判定与重命名")
    candidates = _rank(candidates, probes)

    # 只收录「实测出速度」的节点。speed=0 有两种含义：节点不可用，或者压根没被测到；
    # 两种写进名字里都是 0.0MB/S，对使用者没有意义。
    # 实测过一轮：测速前截断到 400 时，最终 100 个里有 21 个是 0.0MB/S —— 就是这条没做。
    # 判据用「显示值」而不是原始值，否则 0.004 MB/S 会漏过去变成 0.0MB/S。
    if probes:
        measured = [p for p in candidates if _shown_speed(_speed_of(p, probes)) > 0]
        if measured:
            log(f"  实测有速度的节点 {len(measured)}/{len(candidates)}，"
                f"丢弃 {len(candidates) - len(measured)} 个显示为 0.0MB/S 的节点")
            candidates = measured

    code_of = _resolve_countries(candidates, probes, base=pre_codes, log=log)
    if max_nodes > 0:
        candidates = _stratified(
            candidates, max_nodes, code_of, key=lambda p: -_speed_of(p, probes)
        )

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
        bucket.sort(key=lambda p: -_speed_of(p, probes))
        if top_per_country > 0:
            bucket = bucket[:top_per_country]
        for index, proxy in enumerate(bucket, 1):
            original = proxy["name"]
            speed = _speed_of(proxy, probes)
            new_name = f"{label}-{index:02d}-{speed:.1f}MB/S"
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
                "speed_mbps": round(speed, 2),
                "exit_ip": probes.get(original, {}).get("exit_ip"),
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


def _speed_of(proxy: dict, probes: dict[str, dict]) -> float:
    return float(probes.get(proxy["name"], {}).get("speed") or 0.0)


def _shown_speed(speed: float) -> float:
    """节点名里实际会显示出来的速度值（保留 1 位小数）。

    过滤必须按「显示值」而不是「原始值」：实测有一个节点原始速度 0.004 MB/S，
    原始值 > 0 能过过滤，但名字写成 `XX-01-0.0MB/S` —— 对使用者就是个坏节点。
    """
    return float(f"{speed:.1f}")


def _rank(candidates: list[dict], probes: dict[str, dict]) -> list[dict]:
    """有速度的按速度降序排前面，没速度的保持原有（延迟）顺序排后面。"""
    if not probes:
        return candidates
    with_speed = [p for p in candidates if _speed_of(p, probes) > 0]
    without = [p for p in candidates if _speed_of(p, probes) <= 0]
    with_speed.sort(key=lambda p: -_speed_of(p, probes))
    return with_speed + without


def _stratified(items: list[dict], limit: int, code_of: dict[str, str], *, key) -> list[dict]:
    """按国家分层截断：每个国家先分一份配额，再用剩下的名额按 key 补齐。

    目的：避免"延迟最低的前 N 个"被 runner 所在地区整体占满。
    每个国家至少能分到 max(1, limit // 国家数) 个名额。
    """
    if limit <= 0 or len(items) <= limit:
        return items
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        buckets[code_of.get(item["name"], "??")].append(item)

    quota = max(1, limit // len(buckets))
    picked: list[dict] = []
    rest: list[dict] = []
    for code in sorted(buckets, key=lambda c: (-len(buckets[c]), c)):
        bucket = buckets[code]
        picked.extend(bucket[:quota])
        rest.extend(bucket[quota:])
    if len(picked) < limit:
        rest.sort(key=key)
        picked.extend(rest[:limit - len(picked)])
    return sorted(picked, key=key)


def _server_countries(proxies: list[dict], *, log=print) -> dict[str, str]:
    """用「服务器 IP」粗判国家，只用于分层取样，不用于最终命名。

    比出口 IP 粗糙（CDN 场景会判成边缘节点所在地），但便宜得多，
    而且是测速之前就能拿到的唯一国家信息。
    """
    if not proxies:
        return {}
    resolved = geoip.resolve_many([p["server"] for p in proxies], log=log)
    geo = geoip.lookup_batch(list(resolved.values()), log=log)
    out: dict[str, str] = {}
    for proxy in proxies:
        ip = resolved.get(proxy["server"])
        hit = geo.get(ip) if ip else None
        if hit and hit[0]:
            out[proxy["name"]] = hit[0]
    log(f"  服务端 IP 定位成功 {len(out)}/{len(proxies)}")
    return out


def _resolve_countries(candidates: list[dict], probes: dict[str, dict],
                       *, base: dict[str, str] | None = None,
                       log=print) -> dict[str, str]:
    """国家判定优先级：出口 IP > 服务端 IP > 节点名里的地区标签。"""
    code_of: dict[str, str] = {}

    # 1. 出口 IP（最准）
    exit_map = {
        name: item["exit_ip"]
        for name, item in probes.items()
        if item.get("exit_ip")
    }
    if exit_map:
        geo = geoip.lookup_batch(list(exit_map.values()), log=log)
        for name, ip in exit_map.items():
            hit = geo.get(ip)
            if hit and hit[0]:
                code_of[name] = hit[0]
    log(f"  出口 IP 定位成功 {len(code_of)}")

    # 2. 服务端 IP 兜底（复用测速前那次查询，避免重复请求）
    for proxy in candidates:
        name = proxy["name"]
        if name in code_of:
            continue
        hint = (base or {}).get(name)
        if hint:
            code_of[name] = hint

    # 3. 节点名兜底
    for proxy in candidates:
        if proxy["name"] in code_of:
            continue
        hint = country_mod.code_from_name(proxy.get("name") or "")
        if hint:
            code_of[proxy["name"]] = hint

    located = sum(1 for p in candidates if p["name"] in code_of)
    log(f"  最终定位成功 {located}/{len(candidates)}")
    return code_of


# --------------------------------------------------------------------------- 输出


def _header(result: BuildResult) -> str:
    lines = [
        "# ============================================================",
        "# 免费节点聚合订阅 · 自动生成，请勿手动编辑",
        f"# 生成时间：{result.generated_at}",
        f"# 节点数量：{result.stats.accepted}",
        f"# 覆盖地区：{'、'.join(f'{k}×{v}' for k, v in result.countries.items())}",
        "# 节点名格式：国家-序号-实测峰值速度（MB/S）",
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
        f"| 去重后 | {stats.unique}（去掉重复 {stats.duplicates}） |",
        f"| TCP 端口存活 | {stats.tcp_alive} |",
        f"| 内核可加载 | {stats.loadable} |",
        f"| 延迟可用 | {stats.delay_ok} |",
        f"| 下载测速可用 | {stats.speed_ok} |",
        f"| 最终收录 | {stats.accepted} |",
        "",
        "## 地区分布",
        "",
        "| 地区 | 节点数 | 峰值速度 |",
        "| --- | ---: | ---: |",
    ]
    best: dict[str, float] = {}
    for record in result.records:
        cur = best.get(record["country"])
        if cur is None or record["speed_mbps"] > cur:
            best[record["country"]] = record["speed_mbps"]
    for name, count in result.countries.items():
        speed = best.get(name)
        lines.append(f"| {name} | {count} | {f'{speed:.1f} MB/S' if speed else '—'} |")

    lines += [
        "",
        "## 速度最快的 15 个节点",
        "",
        "| 名称 | 类型 | 峰值速度 | 延迟 | 出口 IP |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    ranked = sorted(result.records, key=lambda r: -r["speed_mbps"])[:15]
    for record in ranked:
        delay = f"{record['delay']} ms" if record.get("delay") else "—"
        lines.append(
            f"| {record['name']} | {record['type']} | {record['speed_mbps']:.1f} MB/S | "
            f"{delay} | {record.get('exit_ip') or '—'} |"
        )

    lines += ["", "## 源站", ""]
    for source in result.sources:
        if source["error"]:
            state = f"失败：{source['error']}"
        elif source.get("note"):
            state = f"OK（{source['note']}）"
        else:
            state = "OK"
        lines.append(f"- `{source['id']}`：{source['count']} 个候选 — {state}")
    lines.append("")
    return "\n".join(lines)
