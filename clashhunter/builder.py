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
from .mihomo import REACH_TARGETS, SPEED_URL, TEST_URL, Mihomo, ensure_binary

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
    merged_exit: int = 0
    accepted: int = 0


@dataclass
class BuildResult:
    proxies: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    sources: list[dict] = field(default_factory=list)
    countries: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""
    # 测速机所在位置。速度是「这台机器 → 节点」的实测值，换个地方测就是另一个数，
    # 所以必须写进订阅文件头部，否则使用者会把它当成自己那边的速度。
    vantage: str = ""
    # 多目标可达性分布：{通过目标数: 节点数}。纯诊断，不参与筛选。
    reach: dict[int, int] = field(default_factory=dict)


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


def _reachability(mh: Mihomo, proxies: list[dict], *, timeout_ms: int,
                  workers: int, log=print) -> dict[str, list[str]]:
    """对每个节点跑一遍多个目标，返回 {节点名: [通过的目标名]}。

    为什么要这一步：主延迟只用 gstatic（Google）一个目标，而「能通 Google」
    不等于「能通微软 / Apple / Cloudflare」—— 有些免费节点专门针对 Google 优化过，
    换一个站就打不开。这一轮**只记录、不剔除**：先看真实分布，再决定要不要拿它筛。

    ⚠️ 只能判断「有没有响应」，判断不了「响应内容对不对」——
    mihomo 的 delay API 只回耗时。所以被劫持的 200 也会算通过。
    """
    total = len(proxies)
    if not total:
        return {}
    jobs = [(p["name"], label, url) for p in proxies for label, url in REACH_TARGETS]
    log(f"  多目标可达性：{total} 个节点 × {len(REACH_TARGETS)} 个目标 = {len(jobs)} 次探测")

    hits: dict[str, list[str]] = {p["name"]: [] for p in proxies}

    def job(name: str, label: str, url: str):
        return name, label, mh.delay(name, url, timeout_ms)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, *item) for item in jobs]
        for future in as_completed(futures):
            try:
                name, label, value = future.result()
            except Exception:  # noqa: BLE001
                continue
            if value:
                hits[name].append(label)
            done += 1
            if done % 500 == 0 or done == len(jobs):
                log(f"    ... {done}/{len(jobs)}")
    return hits


def _report_reachability(hits: dict[str, list[str]], *, log=print) -> dict[int, int]:
    """把可达性矩阵压成分布，并指出「只通一个目标」的节点分别通的是哪个。

    后者是关键：如果大量节点只通 google，说明它们专门针对 Google 优化，
    对使用者来说换个站就打不开 —— 这类节点现在完全看不出来。
    """
    if not hits:
        return {}
    total = len(hits)
    dist: dict[int, int] = defaultdict(int)
    for labels in hits.values():
        dist[len(labels)] += 1
    full = len(REACH_TARGETS)
    log(f"  可达目标数分布（{total} 个节点）：")
    for n in range(full, -1, -1):
        count = dist.get(n, 0)
        if not count:
            continue
        bar = "█" * max(1, round(count / total * 36))
        log(f"    {n}/{full}  {count:>5} 个 {count / total * 100:>5.1f}%  {bar}")
    lone = [labels[0] for labels in hits.values() if len(labels) == 1]
    if lone:
        from collections import Counter

        tally = Counter(lone)
        detail = "、".join(f"{k} {v} 个" for k, v in tally.most_common())
        log(f"  只通一个目标的 {len(lone)} 个：{detail}")
    return dict(dist)


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
    # 多目标可达性诊断（Google / Cloudflare / 微软 / Apple）。默认开，纯诊断不筛选。
    reach_check: bool = True,
    speed_url: str = SPEED_URL,
    speed_bytes: int = 6_000_000,
    speed_timeout: float = 10.0,
    speed_workers: int = 32,
    speed_max: int = 0,
    refine_top: int = 300,
    refine_workers: int = 16,
    # ⚠️ 默认关闭。两轮实测（run #12/#13）都没拿到可信数据：
    # #12 跳板 5.1 MB/s（比样本最慢的还慢），判据被压平却报了 9 个假阳性；
    # #13 跳板 11.1 MB/s，40 个样本里 27 个经跳板完全测不出，比值中位数 0.30。
    # 免费节点池里没有质量足够好的亚太跳板 —— 这不是代码能修的。
    # 想再试：--apac-check-top 40 --apac-min-hop 8，并看日志里「无分辨力」有没有出现。
    apac_check_top: int = 0,
    apac_check_workers: int = 1,
    apac_min_hop: float = 8.0,
    ip_timeout: float = 6.0,
    max_nodes: int = 80,
    country_share: float = 0.2,
    top_per_country: int = 0,
    no_speedtest: bool = False,
    vantage: str = "",
    log=print,
) -> BuildResult:
    started = time.time()
    result = BuildResult(vantage=vantage)

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

            # 多目标可达性：纯诊断，不参与筛选。
            # 主延迟只问 gstatic（Google）一个目标，「能通 Google」不等于「能通别的站」。
            # 这一轮只报告分布，先看有多少节点是「只通一个站」的，再决定要不要拿它筛。
            if reach_check and candidates:
                hits = _reachability(mh, candidates, timeout_ms=delay_timeout_ms,
                                     workers=delay_workers, log=log)
                result.reach = _report_reachability(hits, log=log)
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

    # 同一个出口只留最快的那个 —— 必须在分层取样之前做，
    # 这样腾出来的名额才能被别的出口用上。
    _before_merge = len(candidates)
    candidates = _dedupe_by_exit(candidates, probes, log=log)
    result.stats.merged_exit = _before_merge - len(candidates)

    code_of = _resolve_countries(candidates, probes, base=pre_codes, log=log)

    # 亚太视角交叉验证：直连测速是「测速机 → 节点 → 目标站」，而使用者走的是
    # 「国内 → 节点 → 目标站」。两段路不同，所以必须拿一台亚太节点当跳板对照一次，
    # 否则「美西 112 MB/s、亚太 0.5 MB/s」这种节点在产物里完全看不出问题。
    # probe_all 自带 start/stop，出了上面的 try 也能直接再调。
    apac: dict[str, dict] = {}
    if not no_speedtest and apac_check_top > 0:
        apac = _apac_check(
            mh, candidates, probes, code_of,
            top=apac_check_top, workers=apac_check_workers,
            min_hop=apac_min_hop,
            speed_url=speed_url, speed_bytes=speed_bytes,
            speed_timeout=speed_timeout, log=log,
        )

    if max_nodes > 0:
        # 单国上限：名额越少、国家越多，配额就越接近 1，越多名额落到「纯速度」上，
        # 而测速机在美国 —— 取 80 个时美国曾占到 30/80 = 37.5%。
        cap = max(1, round(max_nodes * country_share)) if country_share > 0 else 0
        candidates = _stratified(
            candidates, max_nodes, code_of, key=lambda p: -_speed_of(p, probes),
            cap_per_country=cap, log=log,
        )
        if cap:
            log(f"  单国上限 {cap} 个（{country_share:.0%} × {max_nodes}），"
                f"超额部分让给别的国家")

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


# 跳板必须落在这里，「亚太视角」才成立。含大洋洲与南亚：它们到国内的路径
# 和到美西完全不同，正是要拿来对照的。
APAC_CODES = frozenset({
    "JP", "HK", "TW", "MO", "SG", "KR", "TH", "VN", "MY", "ID", "PH",
    "IN", "AU", "NZ", "KH", "MM", "LA", "BN", "MN", "KZ", "PK", "BD",
    "LK", "NP",
})


def _apac_check(mh, candidates: list[dict], probes: dict[str, dict],
                code_of: dict[str, str], *, top: int, workers: int,
                min_hop: float, speed_url: str, speed_bytes: int,
                speed_timeout: float, log=print) -> dict[str, dict]:
    """经一台亚太节点当跳板复测最快的 top 个，找出「美西快、亚太慢」的节点。

    为什么需要它：直连测速走的是「测速机 → 节点 → 目标站」，而中国用户走的是
    「国内 → 节点 → 目标站」。两段路不同，所以一个从美西看 112 MB/s 的节点，
    从亚太看可能只有 0.5 MB/s —— 现有流程对此完全无感。

    ⚠️ 本轮**只记录、不剔除**。串联链路的速度取最慢的一段，而「本机 → 跳板」
    这一段就在路径里 ⇒ 跳板自身速度就是天花板。

    ⚠️ **跳板的上行带宽才是真瓶颈，不是它的下载速度。** 数据流是
    「目标节点 → 跳板 → 本机」，跳板要把收到的再上传一次。机场节点的上行普遍
    远低于下行，实测 run #12：跳板直连 5.1 MB/s，但经它测 120 个节点，**没有一个
    超过 1.0**，比值中位数 0.09 —— 全被链路本身压平，判据完全失效。
    所以这里必须做两件事：并发压到 1（不能分摊那点上行），以及**分辨力自检**
    （见下面的 `best_via / hop_speed`）。
    """
    pool = [
        p for p in candidates
        if code_of.get(p["name"]) in APAC_CODES and _speed_of(p, probes) > 0
    ]
    if not pool:
        log("  亚太候选池为空（没有测出速度的亚太节点），跳过交叉验证")
        return {}
    pool.sort(key=lambda p: -_speed_of(p, probes))
    hop = pool[0]
    hop_speed = _speed_of(hop, probes)

    sample = [
        p for p in sorted(candidates, key=lambda p: -_speed_of(p, probes))
        if p["name"] != hop["name"]
    ][:top]
    if not sample:
        return {}

    # 跳板比被测样本还慢，这一轮注定测不出东西：拿 5 MB/s 的跳板去验证
    # 一堆直连 20 MB/s 的节点，测出来的只会是跳板自己的上限。
    fastest_direct = max(_speed_of(p, probes) for p in sample)
    if hop_speed < min_hop:
        log(f"  亚太跳板 {hop['name']} 只有 {hop_speed:.1f} MB/s，"
            f"低于门槛 {min_hop:.1f} ⇒ 跳过交叉验证")
        log(f"    理由：样本里最快的直连是 {fastest_direct:.1f} MB/s，"
            f"跳板比它慢 {fastest_direct / max(hop_speed, 0.01):.0f} 倍，"
            f"测出来只会是跳板自己的上限")
        return {}
    log(f"  亚太跳板选 {hop['name']}（直连 {hop_speed:.1f} MB/s，"
        f"候选池 {len(pool)} 台）—— 它的上行带宽就是本轮天花板")

    # 复测不需要 6 MB：经跳板的读数上限就是跳板速度，2 MB 足够，
    # 而且不会撞 0.05s 分母下限（2 MB / 0.05s = 38 MB/s，远高于任何跳板天花板）。
    check_bytes = min(speed_bytes, 2_000_000)
    log(f"  经跳板复测 {len(sample)} 个（并发 {workers}，每个 {check_bytes / 1e6:.0f}MB），"
        f"用来识别「直连快但亚太慢」的节点")
    again = mh.probe_all(
        sample,
        speed_url=speed_url, speed_bytes=check_bytes,
        speed_timeout=speed_timeout, ip_timeout=0.0,
        workers=workers, hop=hop, log=log,
    )

    ratios: list[float] = []
    ceiling_hits = 0
    false_pos: list[tuple[str, float, float]] = []
    best_via = 0.0
    dead_via = 0
    for proxy in sample:
        name = proxy["name"]
        direct = _speed_of(proxy, probes)
        via = float(again.get(name, {}).get("speed") or 0.0)
        if via <= 0 or direct <= 0:
            if direct > 0:
                dead_via += 1
            continue
        best_via = max(best_via, via)
        ratios.append(via / direct)
        if via >= hop_speed * 0.9:
            ceiling_hits += 1
        # 直连不慢、亚太侧却掉到 1 MB/s 以下 —— 1.0 远低于天花板，
        # 说明这个低值不是被天花板压出来的，是真的慢。
        if direct >= 3.0 and via < 1.0:
            false_pos.append((name, direct, via))

    if dead_via:
        # 这个数必须打出来：run #13 是 27/40。它可能意味着「这些节点从亚太确实不通」，
        # 也可能只是跳板不稳 —— 单看这个数分不开，但藏着它就永远发现不了。
        log(f"  经跳板完全测不出速度 {dead_via}/{len(sample)} 个"
            f"（要么跳板不稳，要么这些节点从亚太确实不通）")

    if not ratios:
        log("  ⚠️ 经跳板一个都没测出速度，本轮无结论（跳板可能不可用）")
        return again

    ratios.sort()
    log(f"  亚太/直连 比值中位数 {ratios[len(ratios) // 2]:.2f}，"
        f"{ceiling_hits}/{len(ratios)} 个撞到天花板"
        f"（≥{hop_speed * 0.9:.1f} MB/s，这些的真实值测不出来）")

    # 分辨力自检：如果经跳板最快的那个都够不到天花板的一半，说明瓶颈在链路
    # 本身（跳板上行 / 并发分摊），所有读数都被压平 ⇒ 不能拿它判假阳性。
    # run #12 就是这样：跳板 5.1，最快只测到 1.0（比值 0.20），却报了 9 个「假阳性」。
    if best_via < hop_speed * 0.5:
        log(f"  ⚠️ 本轮无分辨力，不输出假阳性判断：经跳板最快只测到 "
            f"{best_via:.1f} MB/s，而跳板直连 {hop_speed:.1f} MB/s"
            f"（比值 {best_via / hop_speed:.2f} < 0.50）")
        log("    说明瓶颈在链路本身（跳板上行带宽，或并发分摊），"
            "读数被压平后无法区分「节点慢」和「链路慢」")
        return again

    if false_pos:
        log(f"  ⚠️ 疑似假阳性 {len(false_pos)} 个"
            f"（直连 ≥3 但亚太 <1 MB/s，本轮只报告不剔除）：")
        for name, direct, via in false_pos[:8]:
            log(f"      {name}  直连 {direct:.1f} → 亚太 {via:.1f}")
        if len(false_pos) > 8:
            log(f"      ……另有 {len(false_pos) - 8} 个")
    else:
        log("  没有发现「直连快但亚太慢」的节点")
    return again


def _dedupe_by_exit(candidates: list[dict], probes: dict[str, dict], *, log=print) -> list[dict]:
    """同一个出口 IP 只留最快的那一个。

    为什么还需要这一步：`nodes.dedupe()` 的键是 (type, server, port, 凭证)，
    所以「同一台服务器、同一个端口，只是凭证不同」的两条会被当成两个节点。
    实测 run #7 的最终 100 个里，有 7 组共 14 条其实是同一个出口：

        138.199.35.215:9002   美国-07(9.9)   / 美国-13(9.6)      ← 同 server:port
        213.183.63.60:9028/85 保加利亚-01(2.8) / 保加利亚-02(2.6)  ← 同机不同端口
        102.244.78.61:8086/84 喀麦隆-01(0.1)  / 喀麦隆-02(0.1)    ← 同机不同端口
        185.135.86.175:9034/07 拉脱维亚-01(3.0) / 拉脱维亚-02(2.9)
        103.86.116.118        印度尼西亚-01(2.5) / 印度尼西亚-02(1.6)
        62.197.156.21         斯里兰卡-01(2.8) / 斯里兰卡-02(2.5)  ← 一台中继一台落地
        103.86.49.57          泰国-01(3.9)   / 泰国-02(2.7)      ← 两条 CF 前置

    同一个出口的带宽和故障是共享的，留两条等于把 100 个名额里的一个白扔。

    没有出口 IP 时退到「同一个 server:port」—— 那也肯定是同一条管道。
    实测 瑞士-01/瑞士-02 就是 116.204.164.17:7443 的两条，两条都没测出出口 IP，
    不做这个兜底就会被当成两个节点留下来。
    """
    if not probes:
        return candidates
    best: dict[str, dict] = {}
    for proxy in candidates:
        exit_ip = probes.get(proxy["name"], {}).get("exit_ip")
        if exit_ip:
            key = f"exit:{exit_ip}"
        else:
            key = (f"endpoint:{proxy['type']}:"
                   f"{str(proxy['server']).lower()}:{proxy['port']}")
        current = best.get(key)
        if current is None or _speed_of(proxy, probes) > _speed_of(current, probes):
            best[key] = proxy
    merged = list(best.values())
    dropped = len(candidates) - len(merged)
    if dropped:
        log(f"  同一出口合并 {dropped} 个（{len(candidates)} → {len(merged)}，"
            f"每个出口只留最快的一个）")
    return merged


def _rank(candidates: list[dict], probes: dict[str, dict]) -> list[dict]:
    """有速度的按速度降序排前面，没速度的保持原有（延迟）顺序排后面。"""
    if not probes:
        return candidates
    with_speed = [p for p in candidates if _speed_of(p, probes) > 0]
    without = [p for p in candidates if _speed_of(p, probes) <= 0]
    with_speed.sort(key=lambda p: -_speed_of(p, probes))
    return with_speed + without


def _stratified(items: list[dict], limit: int, code_of: dict[str, str], *, key,
                cap_per_country: int = 0, log=print) -> list[dict]:
    """按国家分层截断：每个国家先分一份配额，再用剩下的名额按 key 补齐。

    目的：避免"延迟最低的前 N 个"被 runner 所在地区整体占满。
    每个国家至少能分到 max(1, limit // 国家数) 个名额。

    `cap_per_country` 是「单个国家最多占几个」的上限，0 表示不限。它补的是配额算法
    在**名额少、国家多**时的漏洞：配额是 `floor(limit / 国家数)`，国家数 47 时
    取 100 个配额是 2（保底用掉 94，只剩 6 个按速度补），取 80 个配额就掉到 1
    （保底只用掉 47，剩 33 个全按速度补）—— 而测速机在美国，于是美国占 30/80 = 37.5%。
    加了上限之后，超出的部分会被跳过、让给别的国家，但**总数仍然补到 limit**。
    """
    if limit <= 0 or len(items) <= limit:
        return items
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        buckets[code_of.get(item["name"], "??")].append(item)

    quota = max(1, limit // len(buckets))
    if cap_per_country > 0:
        quota = min(quota, cap_per_country)

    picked: list[dict] = []
    rest: list[dict] = []
    taken: dict[str, int] = defaultdict(int)
    for code in sorted(buckets, key=lambda c: (-len(buckets[c]), c)):
        bucket = buckets[code]
        head = bucket[:quota]
        picked.extend(head)
        taken[code] = len(head)
        rest.extend(bucket[quota:])

    if len(picked) < limit:
        rest.sort(key=key)
        for item in rest:
            if len(picked) >= limit:
                break
            code = code_of.get(item["name"], "??")
            if cap_per_country > 0 and taken[code] >= cap_per_country:
                continue
            picked.append(item)
            taken[code] += 1
        if len(picked) < limit:
            log(f"  [warn] 单国上限 {cap_per_country} 卡住了，只凑到 {len(picked)}/{limit} 个"
                f"（候选里没有更多国家可补）")
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
        "# 节点名格式：国家-序号-实测平均速度（MB/S）",
    ]
    # 速度是「测速机 → 节点」的单点实测值，换一台机器测就是另一个数。
    # 不写清测速机在哪，使用者会把它当成自己那边的速度。
    if result.vantage:
        lines.append(f"# 测速位置：{result.vantage}"
                     "（该速度是这台机器到节点的实测值，不代表你所在网络的速度）")
    lines += [
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
        (f"- 测速位置：**{result.vantage}**（速度是该机器到节点的实测值）"
         if result.vantage else "- 测速位置：未记录"),
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
        f"| 同出口合并 | -{stats.merged_exit}（同一出口只留最快的那个） |",
        f"| 最终收录 | {stats.accepted} |",
        "",
    ]
    if result.reach:
        # 只诊断、不筛选。主延迟只问 gstatic（Google）一个目标，
        # 「能通 Google」不等于「能通别的站」—— 这张表把差距摆出来。
        full = len(REACH_TARGETS)
        total_reach = sum(result.reach.values())
        lines.extend([
            "## 多目标可达性（诊断用，不参与筛选）",
            "",
            f"每个节点分别访问 {full} 个目标："
            + "、".join(label for label, _ in REACH_TARGETS) + "。",
            "",
            "| 通过目标数 | 节点数 | 占比 |",
            "| ---: | ---: | ---: |",
        ])
        for n in range(full, -1, -1):
            count = result.reach.get(n, 0)
            if count:
                lines.append(f"| {n}/{full} | {count} | {count / total_reach:.1%} |")
        lines.append("")
    lines.extend([
        "## 地区分布",
        "",
        "| 地区 | 节点数 | 平均速度 |",
        "| --- | ---: | ---: |",
    ])
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
        "| 名称 | 类型 | 平均速度 | 延迟 | 出口 IP |",
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
