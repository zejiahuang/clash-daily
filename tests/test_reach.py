"""多目标可达性诊断的统计逻辑。

为什么值得单独测：这段只产出「分布」和「只通一个目标的分别是哪个」，
没有它主流程照样跑通，所以错了也不会报错 —— 只会让人得出错误结论。

最要紧的是「只通一个目标」的分组：如果大量节点只通 google，说明它们专门
针对 Google 优化过，换个站就打不开。这个数错位了，整张表就没有意义。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter.builder import _report_reachability  # noqa: E402
from clashhunter.mihomo import REACH_TARGETS  # noqa: E402


def test_distribution(problems: list[str]) -> None:
    hits = {
        "a": ["google", "cloudflare", "microsoft", "apple"],
        "b": ["google", "cloudflare", "microsoft"],
        "c": ["google", "cloudflare"],
        "d": ["google"],
        "e": ["google"],
        "f": ["apple"],
        "g": [],
    }
    lines: list[str] = []
    dist = _report_reachability(hits, log=lambda msg="": lines.append(msg))

    expect = {4: 1, 3: 1, 2: 1, 1: 3, 0: 1}
    if dist != expect:
        problems.append(f"分布算错了：{dist}，应为 {expect}")

    # 分布的总和必须等于节点数，否则有节点被吞了
    if sum(dist.values()) != len(hits):
        problems.append(f"分布合计 {sum(dist.values())} != 节点数 {len(hits)}")

    text = "\n".join(lines)
    # 只通一个目标的是 d、e（google）和 f（apple）—— 分组必须按目标名分开
    if "只通一个目标的 3 个" not in text:
        problems.append(f"「只通一个目标」的数量不对：\n{text}")
    if "google 2 个" not in text:
        problems.append(f"没有按目标分组统计 google：\n{text}")
    if "apple 1 个" not in text:
        problems.append(f"没有按目标分组统计 apple：\n{text}")


def test_empty(problems: list[str]) -> None:
    if _report_reachability({}, log=lambda *a, **k: None) != {}:
        problems.append("空输入应返回空字典")


def test_targets_sane(problems: list[str]) -> None:
    """目标必须是 http 小端点：https 握手会掩盖网络层延迟，大响应体会被带宽拖慢。"""
    if len(REACH_TARGETS) < 3:
        problems.append("目标少于 3 个，分不出「只通一个」")
    labels = [label for label, _ in REACH_TARGETS]
    if len(set(labels)) != len(labels):
        problems.append(f"目标名重复：{labels}")
    for label, url in REACH_TARGETS:
        if not url.startswith("http://"):
            problems.append(f"{label} 用了 https，TLS 握手会让延迟读数偏高")
        if "generate_204" not in url and not url.endswith((".txt", ".html")):
            problems.append(f"{label} 的目标不是小端点：{url}")


def main() -> int:
    problems: list[str] = []
    test_distribution(problems)
    test_empty(problems)
    test_targets_sane(problems)
    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print(f"✓ 可达性诊断通过：分布与分组统计正确，{len(REACH_TARGETS)} 个目标都是 http 小端点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
