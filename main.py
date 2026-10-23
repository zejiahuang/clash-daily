#!/usr/bin/env python3
"""命令行入口。

本地试跑（不做真实测速，只验证采集与解析）：
    python main.py --no-speedtest --max-nodes 50

完整跑一次：
    python main.py --out dist
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from clashhunter import builder
from clashhunter.mihomo import TEST_URL


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="每日免费节点聚合：抓取最新订阅、测速、按出口国家重命名、生成 Clash 配置",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", default="dist", help="输出目录")
    parser.add_argument("--work", default="work", help="临时工作目录（放内核和中间配置）")
    parser.add_argument("--sources", default="",
                        help="只跑指定源站，逗号分隔：manifest,clash-node,v2rayfree,stelliberty")
    parser.add_argument("--max-articles", type=int, default=3,
                        help="每个索引站最多回看几篇文章（当天文章没抓到就往前翻）")
    parser.add_argument("--tcp-timeout", type=float, default=3.0, help="TCP 预筛单端点超时(秒)")
    parser.add_argument("--tcp-workers", type=int, default=128, help="TCP 预筛并发")
    parser.add_argument("--delay-url", default=TEST_URL, help="测速用的探测 URL")
    parser.add_argument("--delay-timeout", type=int, default=5000, help="单节点测速超时(毫秒)")
    parser.add_argument("--delay-workers", type=int, default=32, help="测速并发")
    parser.add_argument("--max-nodes", type=int, default=300, help="最终最多保留多少个节点")
    parser.add_argument("--top-per-country", type=int, default=0,
                        help="每个国家最多保留几个（0 表示不限制）")
    parser.add_argument("--exit-ip-max", type=int, default=300,
                        help="最多对多少个节点做出口 IP 探测（串行，很慢）")
    parser.add_argument("--exit-ip-timeout", type=float, default=6.0, help="出口 IP 探测超时(秒)")
    parser.add_argument("--no-exit-ip", action="store_true",
                        help="跳过出口 IP 探测，国家改用服务端 IP / 节点名推断")
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

    log("=" * 62)
    log("免费节点每日聚合")
    log("=" * 62)

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
            max_nodes=args.max_nodes,
            exit_ip_max=args.exit_ip_max,
            exit_ip_timeout=args.exit_ip_timeout,
            no_exit_ip=args.no_exit_ip,
            no_speedtest=args.no_speedtest,
            top_per_country=args.top_per_country,
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
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(_step_summary(result))
    return 0


def _step_summary(result) -> str:
    stats = result.stats
    lines = [
        "## 免费节点聚合结果",
        "",
        f"- 最终可用：**{stats.accepted}** 个节点，覆盖 **{len(result.countries)}** 个国家/地区",
        f"- 采集 {stats.raw} → 去重 {stats.unique} → TCP 存活 {stats.tcp_alive} → "
        f"可加载 {stats.loadable} → 真实可用 {stats.alive}",
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
