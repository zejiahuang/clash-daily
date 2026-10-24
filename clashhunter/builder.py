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
    speed_bytes: int = 6_000_000,
    speed_timeout: float = 10.0,
    speed_workers: int = 32,
    speed_max: int = 0,
    refine_top: int = 300,
    refine_workers: int = 16,
    # 复测单独用更大的下载量：6 MB 会把快节点顶到 max(span, 0.05) 的地板上，
    # 算出来的数字是「除出来的」而不是「测出来的」。算式见 _refine_speed 的 docstring。
    refine_bytes: int = 24_000_000,
    # 复测对几个测速源各测一遍、取中位数。0 = 只用单一 speed_url（旧行为）。
    # 源不写死：由 _pick_sources() 在运行时直连自检决定用哪几个 —— runner 机房每天变。
    speed_sources: int = 3,
    source_min_ratio: float = 0.4,
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

            # 5. 管道自检 → 定初筛参数 → 下载测速 + 出口 IP ------------------
            log("[5/6] 出口管道自检与下载测速")
            # 顺序是有讲究的：自检量的是「runner 的出口有多宽」，而初筛的并发数和
            # 每节点下载量都该由这个数决定。顺序反了，就是拿 32 并发 × 6 MB 去压
            # 一条不知道多宽的管子 —— 压不动就整片超时。
            pipe, _single = _measure_pipe(
                mh, url=speed_url, conns=PIPE_PROBE_CONNS,
                nbytes=min(refine_bytes, PIPE_PROBE_BYTES),
                timeout=min(speed_timeout, PIPE_PROBE_TIMEOUT), log=log,
            )
            screen_workers, screen_bytes = _fit_to_pipe(
                pipe, speed_workers, speed_bytes, speed_timeout, log=log,
            )
            probes = mh.probe_all(
                candidates,
                speed_url=speed_url, speed_bytes=screen_bytes,
                speed_timeout=speed_timeout, ip_timeout=ip_timeout,
                workers=screen_workers, log=log,
            )
            result.stats.speed_ok = sum(1 for v in probes.values() if v.get("speed", 0) > 0)
            log(f"  下载测速可用 {result.stats.speed_ok}/{len(candidates)}")

            # 全灭时给一次机会，且只改一个变量：每节点下载量降到 64 KB 门槛之上。
            # 理由：读数 0 只出现在「一个字节都没读到」或「不到 64 KB」，
            # 与「读得慢」无关 —— 慢只会让读数变小，不会变成 0。缩小下载量能分开
            # 「参数太大来不及」和「这条出口/这批节点根本不通」。两轮都 0 ⇒ 后者。
            if result.stats.speed_ok == 0 and screen_bytes > RETRY_BYTES:
                log(f"  ⚠️ 初筛 {len(candidates)} 个全是 0 ⇒ 改用 "
                    f"{RETRY_BYTES / 1024:.0f} KB 重试一次")
                probes = mh.probe_all(
                    candidates,
                    speed_url=speed_url, speed_bytes=RETRY_BYTES,
                    speed_timeout=speed_timeout, ip_timeout=ip_timeout,
                    workers=screen_workers, log=log,
                )
                result.stats.speed_ok = sum(
                    1 for v in probes.values() if v.get("speed", 0) > 0)
                log(f"  重试后可用 {result.stats.speed_ok}/{len(candidates)}")

            # 复测：低并发 + 更大的下载量 + 多个测速源取中位数，重测「可能进入最终名单」的那批。
            # 为什么必须换更大的下载量、为什么不能沿用初筛的 speed_bytes，
            # 算式与实测依据都在 _refine_speed 的 docstring 里。
            sources: list[tuple[str, str]] = []
            if refine_top > 0 and 0 < pipe < SOURCE_MIN_MBS:
                # 复测要解决的问题是「0.05 秒地板」和「并发抢带宽」造成的低估。
                # 管道比这两个效应都窄时，复测读数一模一样，只是白花时间：
                # 300 × 24 MB = 7.2 GB，按 1 MB/s 是两小时。
                log(f"  ⚠️ 管道只有 {pipe:.2f} MB/s（门槛 {SOURCE_MIN_MBS:.0f}）"
                    f"⇒ 跳过复测（读数被管道压住，换大下载量也不会变）")
                refine_top = 0
            if refine_top > 0:
                # 自检是**串行**的：并行会让几个源互相抢 runner 的出口带宽，
                # 量到的就不是各自的容量了。下载量与超时都压短，最坏 ~40 秒。
                sources = _pick_sources(
                    mh, nbytes=min(refine_bytes, PIPE_PROBE_BYTES),
                    timeout=min(speed_timeout, PIPE_PROBE_TIMEOUT),
                    want=speed_sources, min_ratio=source_min_ratio, log=log,
                )
            improved = _refine_speed(
                mh, candidates, probes,
                top=refine_top, workers=refine_workers,
                speed_url=speed_url, speed_bytes=refine_bytes,
                speed_timeout=speed_timeout, ip_timeout=ip_timeout,
                screen_bytes=screen_bytes, sources=sources, log=log,
            )
            if improved is not None:
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


def _refine_speed(mh, candidates: list[dict], probes: dict[str, dict], *,
                  top: int, workers: int, speed_url: str, speed_bytes: int,
                  speed_timeout: float, ip_timeout: float,
                  screen_bytes: int = 0, sources: list[tuple[str, str]] | None = None,
                  log=print) -> int | None:
    """对初筛最快的一批用低并发 + 更大的下载量重测，原地更新 probes。

    返回「比初筛更快」的个数；`top<=0` 或没有可复测的项时返回 None。

    为什么要单独一轮：初筛并发 32，几十个节点在抢同一条出口带宽，快节点被压低，
    而**写进节点名的就是这个数**。

    为什么要单独的下载量（`speed_bytes` 参数必须传复测量，不能沿用初筛的）：
    `measure_speed()` 的分母是「首字节 → 末字节」，且下限 `max(span, 0.05)`：

        6 MB  ⇒ 上限 5.72 / 0.05 = 114.4 MB/s
                 run #11 头名 112.75，反推 span = 0.0507 秒 —— 正好压在地板上，
                 那个数字不是测出来的，是除出来的，真实值可能是 150 也可能是 500。
        24 MB ⇒ 上限 22.89 / 0.05 = 457.8 MB/s，免费节点够不到，地板不再参与。

    初筛保持小下载量：1400 个 × 24 MB = 33 GB，只为排个序不值得；
    复测 300 个 × 24 MB = 7.2 GB 上限，且每个都被 `speed_timeout` 兜住。

    `sources` 非空时每个节点对每个源各下一份 `speed_bytes`，`speed` 取中位数
    ⇒ 总流量是单源的 `len(sources)` 倍。这是「让多个网站各自说话」的代价，
    可以用 `--speed-sources` 调小。
    """
    if top <= 0:
        return None
    ranked = sorted(candidates, key=lambda p: -_speed_of(p, probes))
    refine = [p for p in ranked[:top] if _speed_of(p, probes) > 0]
    if not refine:
        return None

    hint = f"，每个下 {speed_bytes / 1048576:.0f} MB"
    if screen_bytes > 0 and screen_bytes != speed_bytes:
        hint += (f"（初筛 {screen_bytes / 1048576:.0f} MB，"
                 f"上限从 {screen_bytes / 1048576 / 0.05:.0f} MB/s 抬到 "
                 f"{speed_bytes / 1048576 / 0.05:.0f} MB/s）")
    if sources:
        hint += f"，{len(sources)} 个源取中位数（总流量 ×{len(sources)}）"
    log(f"  复测前 {len(refine)} 个（并发 {workers}{hint}，"
        f"消除初筛抢带宽造成的低估与 0.05 秒地板截断）")

    again = mh.probe_all(
        refine,
        speed_url=speed_url, speed_bytes=speed_bytes,
        speed_timeout=speed_timeout, ip_timeout=ip_timeout,
        workers=workers, sources=sources, log=log,
    )
    improved = 0
    for name, item in again.items():
        # 复测失败不能把初筛的有效结果冲掉。
        if item.get("speed", 0) > 0:
            if item["speed"] > probes.get(name, {}).get("speed", 0):
                improved += 1
            probes[name] = item
    return improved


# 一个测速源自己都跑不到这个速度，经它测出来的节点读数就永远到不了这个值
# —— 那是源的天花板，不是节点的。低于它的一律不采用。
SOURCE_MIN_MBS = 20.0

# 管道自检的固定开销。这三个数一起决定「自检最多花多少秒」：
# 单连接 6 秒 + 并发 6 秒 = 12 秒封顶，慢管子上也不会失控。
PIPE_PROBE_BYTES = 6_000_000
PIPE_PROBE_TIMEOUT = 6.0
PIPE_PROBE_CONNS = 6

# 全灭重试时的每节点下载量：必须高于 measure_speed_ex 的 64 KB 门槛，
# 否则「读到了但太少」会和「什么都没读到」混在一起。
RETRY_BYTES = 262_144


def _measure_pipe(mh, *, url: str, conns: int, nbytes: int, timeout: float,
                  log=print) -> tuple[float, float]:
    """量 runner 的出口管道，返回 `(并发总量, 单连接读数)`，单位 MB/s。

    两个数必须分开量、分开报，因为它们的关系决定「要不要压并发」：
      - 并发总量 ≈ 单连接 ⇒ 出口总带宽就这么宽，再并发也不会变多，必须压；
      - 并发总量 ≫ 单连接 ⇒ 只是每条连接被限到这么宽，并发完全跑得动，不用压。
    只量一个数就把「按连接限速」当成「按出口限速」，是两种相反的结论共用一个
    读数 —— 而这正是 `_fit_to_pipe` 唯一依赖的输入。

    `pipe<=0` 表示量不出来（一个字节都没读到），调用方据此不做任何调整。
    """
    one = mh.measure_aggregate(url, conns=1, max_bytes=nbytes, timeout=timeout)
    single = float(one.get("agg") or 0.0)
    many = mh.measure_aggregate(url, conns=conns, max_bytes=nbytes, timeout=timeout)
    agg = float(many.get("agg") or 0.0)
    log(f"  出口自检：单连接 {single:.2f} MB/s，{conns} 条并发合计 {agg:.2f} MB/s"
        f"（{many['ok']}/{conns} 条有数据，墙钟 {many['wall_s']}s）")
    if agg <= 0:
        errs = many.get("errs") or []
        log("  ⚠️ 并发自检一个字节都没读到 ⇒ 不做调整（沿用默认并发与下载量）"
            + (f"；原因：{errs[0]}" if errs else ""))
        return 0.0, single
    if single > 0 and agg > single * 1.5:
        log(f"     ⇒ 单连接被限速（{agg / single:.1f}×），"
            f"管道按并发总量 {agg:.2f} MB/s 算")
    elif single > 0:
        log(f"     ⇒ 并发没有提升（{agg / single:.1f}×），出口总带宽就是这么多")
    return agg, single


def _fit_to_pipe(pipe: float, workers: int, nbytes: int, timeout: float,
                 *, log=print) -> tuple[int, int]:
    """按「管道有多粗」把并发和每节点下载量压到真能测出数的范围。

    为什么必须有这一步：`workers` 个并发各下 `nbytes`，总量是
    `workers × nbytes`；如果这个量在 `timeout` 内搬不完，读数会被摊薄成
    `管道 / 并发` —— 极端情况下连 64 KB 都凑不到，于是每个节点都是 0。

    `pipe` 必须是**并发总量**（见 `_measure_pipe`），不是单连接读数：
    单连接读数在「按连接限速」的出口上会低估 K 倍。

    规则：
      - 管道 < `SOURCE_MIN_MBS`（20 MB/s）时，**并发取 1**。
        管道是瓶颈时，每连接只能分到 `pipe / workers`，并发只会把读数天花板
        摊薄、把节点之间的差别压平，而整段墙钟（`节点数 × nbytes / pipe`）
        与并发**无关** —— 多开并发只是把同一份带宽分给更多节点。
        代价是失去握手时间的重叠（每个节点约 0.5 s），换来的是读数不被摊平
        —— 产物名字里的速度就是读数，读数平了等于排名没有意义。
      - 每节点下载量取 **1 MB**（不往上抬）。整段墙钟 ≈ `节点数 × nbytes / pipe`，
        所以字节数越小越快；1 MB 给出的读数上限是 `1/0.05 = 20 MB/s`，
        已经不高于这种管道的天花板，再大只是白花时间（5 MB 就是 5 倍墙钟）。
      - 管道够粗时只按 `workers × nbytes / pipe <= timeout/2` 收并发，下载量不动。
    """
    if pipe <= 0 or workers <= 0 or nbytes <= 0 or timeout <= 0:
        return workers, nbytes

    per_node = nbytes / 1048576
    if pipe < SOURCE_MIN_MBS:
        # 只往下压，不往上抬：`--speed-bytes` 比这还小的时候它本来就来得及，
        # 悄悄改大等于覆盖了用户明确给的值。
        # 下限 0.1 MB：再小就接近 measure_speed_ex 的 64 KB 门槛，
        # 「读到了但太少」会和「什么都没读到」混在一起。
        target = min(1.0, max(0.1, pipe * timeout / 2))
        new_bytes = int(min(per_node, target) * 1048576)
        log(f"  ⚠️ 出口管道只有 {pipe:.2f} MB/s（门槛 {SOURCE_MIN_MBS:.0f}）"
            f"⇒ {workers} 并发 × {per_node:.1f} MB 改成 1 并发 × "
            f"{new_bytes / 1e6:.1f} MB")
        log(f"     读数天花板就是管道本身 ≈ {pipe:.2f} MB/s，"
            f"再快的节点也测不过这个数（这是 runner 的带宽，不是节点的问题）")
        return 1, new_bytes

    budget = timeout * 0.5
    if workers * per_node / pipe <= budget:
        return workers, nbytes
    new_workers = max(1, min(workers, int(pipe * budget / per_node)))
    log(f"  ⚠️ {workers} 并发 × {per_node:.1f} MB 在 {pipe:.2f} MB/s 的管道上"
        f"需要 {workers * per_node / pipe:.0f}s，超过 {timeout:.0f}s 超时的一半"
        f" ⇒ 改成 {new_workers} 并发")
    return new_workers, nbytes


def _pick_sources(mh, *, nbytes: int, timeout: float, want: int,
                  min_ratio: float, log=print) -> list[tuple[str, str]]:
    """直连自检每个候选测速源，挑出**当天真的跑得动**的几个。

    为什么必须当场量：runner 所在机房每天变（凤凰城 / 博伊德尔 / 昆西 / 得梅因 /
    圣何塞都出现过），某个源今天从某个机房能不能跑满，事先不可能知道。
    把「哪个源可用」写死在代码里，等于把某一天的测量结果当成永久事实。

    `want<=0` 或一个源都不可用时返回空列表 ⇒ 退回单一 `speed_url`（旧行为）。
    """
    if want <= 0:
        return []
    log(f"  测速源自检（直连，不经节点，每源 {nbytes / 1e6:.0f} MB）")
    rows = mh.measure_sources(max_bytes=nbytes, timeout=timeout, log=log)
    usable = [r for r in rows if r["speed"] > 0]
    if not usable:
        log("  ⚠️ 没有任何测速源能跑通 ⇒ 退回单一源。"
            "若单一源是 Cloudflare，注意 bytes 在 11~20 MB 区间会稳定 403")
        return []

    best = usable[0]["speed"]
    floor = max(SOURCE_MIN_MBS, best * min_ratio)
    keep = [r for r in usable if r["speed"] >= floor][:want]
    if not keep:
        # 所有源都低于门槛（机房出口整体很慢的一天）：至少留最快的那个，
        # 否则整轮退化成单源，反而更差。
        keep = usable[:1]
        log(f"  ⚠️ 最快的源也只有 {best:.1f} MB/s，低于门槛 {floor:.1f} "
            f"⇒ 只留 {keep[0]['label']}，本轮读数会被它压住")

    skipped = [r for r in rows if r not in keep]
    if skipped:
        detail = "、".join(
            f"{r['label']}({r['speed']:.1f})" if r["speed"] > 0
            else f"{r['label']}(不可用)" for r in skipped)
        log(f"  未采用：{detail}")
    log(f"  ⇒ 采用 {len(keep)} 个源取中位数："
        f"{' / '.join(r['label'] for r in keep)}"
        + ("（两源时中位数即均值）" if len(keep) == 2 else ""))
    # ⚠️ 这里的 `best` 是**单连接**读数，不是「管道有多粗」—— 出口按连接限速时
    # 它会低估 K 倍。管道由 _measure_pipe() 用并发总量单独量，见 _fit_to_pipe。
    return [(r["label"], r["url"]) for r in keep]


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
        "## 地区分布",
        "",
        "| 地区 | 节点数 | 平均速度 |",
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
