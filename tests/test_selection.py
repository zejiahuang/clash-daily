"""分层取样回归测试。

背景：测速前如果直接取"延迟最低的前 N 个"，结果会被 runner 所在地区整体占满
（实测在美西 runner 上 200 个结果里 179 个是美国节点）。分层取样用来保证
每个国家都能分到名额。这个逻辑一旦写错，表现是"某些国家整体消失"，
不会报错，所以单独测。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter.builder import _shown_speed, _stratified  # noqa: E402


def build_corpus(dominant: int, others: dict[str, int]) -> tuple[list[dict], dict[str, str]]:
    items: list[dict] = []
    codes: dict[str, str] = {}
    for i in range(dominant):
        name = f"us{i:03d}"
        items.append({"name": name, "v": i})
        codes[name] = "US"
    for offset, (code, count) in enumerate(others.items(), start=1):
        for k in range(count):
            name = f"{code}{k:02d}"
            items.append({"name": name, "v": 1000 + offset * 100 + k})
            codes[name] = code
    return items, codes


def main() -> int:
    problems: list[str] = []

    # 1. 美国 500 条、其余 8 国各 20 条，限 80 个 —— 每国都应拿到配额
    others = {"JP": 20, "HK": 20, "SG": 20, "DE": 20, "GB": 20, "KR": 20, "TW": 20, "FR": 20}
    items, codes = build_corpus(500, others)
    out = _stratified(items, 80, codes, key=lambda p: p["v"])
    dist = Counter(codes[p["name"]] for p in out)
    if len(out) != 80:
        problems.append(f"总数 {len(out)} != 80")
    if len(dist) != 9:
        problems.append(f"只覆盖了 {len(dist)} 个国家，应该是 9")
    if dist.get("US", 0) > 20:
        problems.append(f"美国占 {dist['US']} 个，分层没起到约束作用")
    for code in others:
        if dist.get(code, 0) < 8:
            problems.append(f"{code} 只有 {dist.get(code, 0)} 个，低于配额 8")
    if len({p["name"] for p in out}) != len(out):
        problems.append("输出有重复节点")

    # 2. 小国总数不够时，剩余名额由大国补齐（不报错、数量仍然准确）
    few, few_codes = build_corpus(500, {"JP": 5, "HK": 5, "SG": 5})
    out_few = _stratified(few, 80, few_codes, key=lambda p: p["v"])
    dist_few = Counter(few_codes[p["name"]] for p in out_few)
    if len(out_few) != 80:
        problems.append(f"小国不足时总数 {len(out_few)} != 80")
    for code in ("JP", "HK", "SG"):
        if dist_few.get(code, 0) != 5:
            problems.append(f"{code} 只有 {dist_few.get(code, 0)} 个，小国应全部保留")

    # 3. 数量不足 limit 时原样返回
    small, small_codes = build_corpus(3, {"JP": 2})
    if _stratified(small, 100, small_codes, key=lambda p: p["v"]) != small:
        problems.append("总数小于 limit 时应原样返回")

    # 4. limit <= 0 表示不限制
    if len(_stratified(items, 0, codes, key=lambda p: p["v"])) != len(items):
        problems.append("limit=0 时应返回全部")

    # 5. 未定位到国家的节点归入 ??，也要能拿到名额
    unknown = [{"name": f"x{i}", "v": 5000 + i} for i in range(10)]
    out2 = _stratified(items + unknown, 50, codes, key=lambda p: p["v"])
    if len(out2) != 50:
        problems.append(f"含未定位节点时总数 {len(out2)} != 50")

    # 6. 排序必须遵循 key（升序时最小在前）
    values = [p["v"] for p in out]
    if values != sorted(values):
        problems.append("输出没有按 key 排序")

    # 7. 0 速度过滤必须按「显示值」判断，不能按原始值
    #    背景：实测有一个节点原始速度 0.004 MB/S，原始值 > 0 能过过滤，
    #    但名字会写成 `XX-01-0.0MB/S` —— 对使用者就是个坏节点。
    cases = [
        (0.0, 0.0, "真 0 必须被丢掉"),
        (0.004, 0.0, "0.004 显示成 0.0，必须被丢掉"),
        (0.049, 0.0, "0.049 显示成 0.0，必须被丢掉"),
        (0.05, 0.1, "0.05 显示成 0.1，应保留"),
        (1.04, 1.0, "1.04 显示成 1.0，应保留"),
        (12.55, 12.6, "12.55 显示成 12.6"),
    ]
    for raw, want, why in cases:
        got = _shown_speed(raw)
        if abs(got - want) > 1e-9:
            problems.append(f"_shown_speed({raw}) = {got}，应为 {want}（{why}）")
    # 与节点名里的格式化必须一致
    for raw in (0.0, 0.004, 0.05, 1.04, 12.55, 99.99):
        if f"{raw:.1f}" != f"{_shown_speed(raw):.1f}":
            problems.append(f"_shown_speed({raw}) 与名字里的 f'{raw:.1f}' 不一致")

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 分层取样通过：小国全部保留、大国被配额约束、边界场景正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
