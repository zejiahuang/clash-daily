#!/usr/bin/env python3
"""命令行入口。

本地试跑（不做测速，只验证采集与解析）：
    python main.py --no-speedtest --max-nodes 50

完整跑一次：
    python main.py --out dist
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from clashhunter import builder
from clashhunter import countries as country_mod
from clashhunter.mihomo import SPEED_URL, TEST_URL


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="每日节点聚合工具：抓取公开节点源、测速、按出口地区重命名、生成配置文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", default="dist", help="输出目录")
    parser.add_argument("--work", default="work", help="临时工作目录（放内核和中间配置）")
    parser.add_argument("--sources", default="",
                        help="只跑指定源站，逗号分隔：manifest,clash-node,v2rayfree,stelliberty,"
                             "lancex,nodetoolbox,clashsuburl,zhuhaiuk,tg-nav,freeclashverge")
    parser.add_argument("--max-articles", type=int, default=3,
                        help="每个索引站最多回看几篇文章（当天文章没抓到就往前翻）")

    parser.add_argument("--tcp-timeout", type=float, default=3.0, help="TCP 预筛单端点超时(秒)")
    parser.add_argument("--tcp-workers", type=int, default=128, help="TCP 预筛并发")

    parser.add_argument("--delay-url", default=TEST_URL, help="延迟测速的探测 URL")
    parser.add_argument("--delay-timeout", type=int, default=5000, help="单节点延迟测速超时(毫秒)")
    parser.add_argument("--delay-workers", type=int, default=32, help="延迟测速并发")

    parser.add_argument("--speed-url", default=SPEED_URL, help="下载测速用的 URL")
    parser.add_argument("--speed-bytes", type=int, default=6_000_000,
                        help="每个节点最多下载多少字节用于测速")
    parser.add_argument("--speed-timeout", type=float, default=10.0, help="单节点下载测速超时(秒)")
    parser.add_argument("--speed-workers", type=int, default=32,
                        help="下载测速初筛并发（太高会互相抢带宽、测出来偏低，后面还有复测兜底）")
    parser.add_argument("--speed-max", type=int, default=0,
                        help="测速前最多保留多少个节点（0=不预采样，延迟可用的全部测速）")
    parser.add_argument("--refine-top", type=int, default=300,
                        help="对初筛最快的多少个做低并发复测（0=不复测）")
    parser.add_argument("--refine-workers", type=int, default=16,
                        help="复测并发（故意低于初筛，减少抢带宽造成的低估）")
    parser.add_argument("--ip-timeout", type=float, default=6.0, help="出口 IP 探测超时(秒)")

    parser.add_argument("--max-nodes", type=int, default=80, help="最终最多保留多少个节点")
    parser.add_argument("--country-share", type=float, default=0.2,
                        help="单个国家最多占最终名单的比例（0=不限制）。"
                             "名额少、国家多时配额会退化成 1，剩下的名额全按速度补，"
                             "而测速机在美国，不设上限美国会占到三四成")
    parser.add_argument("--vantage", default="",
                        help="测速机位置，写进订阅文件头部（留空则尝试从 <out>/runner-geo.json 读）")
    parser.add_argument("--top-per-country", type=int, default=0,
                        help="每个国家最多保留几个（0 表示不限制）")
    parser.add_argument("--no-speedtest", action="store_true",
                        help="跳过 mihomo 测速（本地没网时用，仅验证采集链路）")
    parser.add_argument("--quiet", action="store_true", help="少打日志")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    def log(message: str = "") -> None:
        if not args.quiet:
            print(message, flush=True)

    only = {s.strip() for s in args.sources.split(",") if s.strip()} or None
    started = time.time()
    vantage = args.vantage or _detect_vantage(Path(args.out))

    log("=" * 62)
    log("免费节点每日聚合")
    log("=" * 62)
    if vantage:
        log(f"测速位置：{vantage}（写进订阅文件头部）")

    try:
        result = builder.build(
            out_dir=Path(args.out),
            work_dir=Path(args.work),
            only=only,
            max_articles=args.max_articles,
            tcp_timeout=args.tcp_timeout,
            tcp_workers=args.tcp_workers,
            delay_url=args.delay_url,
            delay_timeout_ms=args.delay_timeout,
            delay_workers=args.delay_workers,
            speed_url=args.speed_url,
            speed_bytes=args.speed_bytes,
            speed_timeout=args.speed_timeout,
            speed_workers=args.speed_workers,
            speed_max=args.speed_max,
            refine_top=args.refine_top,
            refine_workers=args.refine_workers,
            ip_timeout=args.ip_timeout,
            max_nodes=args.max_nodes,
            country_share=args.country_share,
            top_per_country=args.top_per_country,
            no_speedtest=args.no_speedtest,
            vantage=vantage,
            log=log,
        )
    except SystemExit as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    paths = builder.write_outputs(result, Path(args.out), log=log)
    if not paths:
        return 1

    log("")
    log("=" * 62)
    log(f"完成：{result.stats.accepted} 个节点，{len(result.countries)} 个国家/地区，"
        f"总耗时 {time.time() - started:.0f}s")
    log("=" * 62)
    for name, count in result.countries.items():
        log(f"  {name}: {count}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(_step_summary(result))
    return 0


def _detect_vantage(out_dir: Path) -> str:
    """从工作流写下的 runner-geo.json 拼出「国家·城市」。

    工作流的「记录测速机位置」步骤在 main.py 之前跑，所以这里直接读文件即可。
    读不到就返回空串 —— 头部只是少一行，不影响构建。
    """
    geo_file = out_dir / "runner-geo.json"
    if not geo_file.is_file():
        return ""
    try:
        geo = json.loads(geo_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    parts = []
    code = str(geo.get("countryCode") or "").strip().upper()
    country = (country_mod.zh_name(code) if code else "") or str(geo.get("country") or "").strip()
    if country:
        parts.append(country)
    city = str(geo.get("city") or "").strip()
    if city:
        parts.append(city)
    return "·".join(parts)


def _step_summary(result) -> str:
    stats = result.stats
    lines = [
        "## 免费节点聚合结果",
        "",
        f"- 最终可用：**{stats.accepted}** 个节点，覆盖 **{len(result.countries)}** 个国家/地区",
        f"- 测速位置：**{result.vantage or '未记录'}**（速度是该机器到节点的实测值）",
        f"- 采集 {stats.raw} → 去重 {stats.unique} → TCP 存活 {stats.tcp_alive} → "
        f"可加载 {stats.loadable} → 延迟可用 {stats.delay_ok} → 下载测速可用 {stats.speed_ok}",
        "",
        "| 地区 | 数量 |",
        "| --- | ---: |",
    ]
    for name, count in result.countries.items():
        lines.append(f"| {name} | {count} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
