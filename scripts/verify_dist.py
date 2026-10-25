#!/usr/bin/env python3
"""校验 dist/clash.yaml 是不是一份「客户端真的能加载」的配置。

为什么单独成文件：断言必须只有一份。以前它写在 workflow 的 heredoc 里，
换平台时只能复制一遍 —— 两份必然走样，而「走样的校验」比没有校验更危险：
它会告诉你通过。

判据都是**客户端会拒绝加载**的硬条件，不是风格问题：
  1. 没有节点 ⇒ 配置无意义
  2. 节点名重复 ⇒ Clash 直接拒绝加载
  3. 名字不符合 `国家-序号-速度MB/S[·净N]` ⇒ 使用者看不出速度
  4. 缺 server/port ⇒ 加载报错
  5. 节点没进「自动选择」组 ⇒ 客户端切到该组时用不上

用法：
    python scripts/verify_dist.py [--path dist/clash.yaml]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# 名字形如 `日本-01-12.5MB/S`，拿到出口 IP 纯净度时带 `·净85` 后缀。
# ⚠️ 后缀必须允许，而且必须是**可选**的：`builder._finalize` 只在真的查到纯净度
#    时才追加它（查不到就留空，见那里的注释）。这里曾经只认到 `MB/S` 结尾 ——
#    于是**每一份正常产物都校验失败**，报错却是「80 个节点名不符合格式」，
#    看起来像数据坏了。判据要跟着命名走，不能各写一份。
NAME_PATTERN = re.compile(
    r"^.+-(\d{2,})-(?P<speed>\d+\.\d)MB/S(?:·净(?P<purity>\d{1,3}))?$")


def verify(path: Path) -> list[str]:
    problems: list[str] = []
    if not path.is_file() or path.stat().st_size == 0:
        return [f"{path} 不存在或为空"]

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    proxies = (doc or {}).get("proxies") or []
    if not proxies:
        return ["配置里没有节点"]

    names = [p.get("name", "") for p in proxies]
    if len(names) != len(set(names)):
        dup = sorted({n for n in names if names.count(n) > 1})
        problems.append(f"节点名有重复（Clash 会拒绝加载）：{dup[:5]}")

    bad = [n for n in names if not NAME_PATTERN.match(n)]
    if bad:
        problems.append(
            f"{len(bad)} 个节点名不符合「国家-序号-速度MB/S[·净N]」格式：{bad[:5]}")

    # 纯净度写在名字里，所以它的取值也是「对外承诺」的一部分：
    # 超出 0~100 说明打分函数坏了，而名字看起来仍然正常。
    # 用具名组取值（而不是 group(3)）：正则哪天被改回去，这里只是「取不到」，
    # 不会 IndexError —— 校验器自己崩掉比校验失败更难查。
    for name in names:
        match = NAME_PATTERN.match(name)
        score = match.groupdict().get("purity") if match else None
        if score and not 0 <= int(score) <= 100:
            problems.append(f"节点名里的纯净度越界（应 0~100）：{name}")

    missing = [n for n, p in zip(names, proxies) if not (p.get("server") and p.get("port"))]
    if missing:
        problems.append(f"{len(missing)} 个节点缺 server/port：{missing[:5]}")

    groups = {g.get("name"): g for g in (doc or {}).get("proxy-groups") or []}
    for required in ("节点选择", "自动选择"):
        if required not in groups:
            problems.append(f"缺少代理组「{required}」")
    if "自动选择" in groups:
        absent = set(names) - set(groups["自动选择"].get("proxies") or [])
        if absent:
            problems.append(f"{len(absent)} 个节点不在「自动选择」组里：{sorted(absent)[:5]}")

    # 全部 0.0 MB/S ⇒ 这份配置对使用者毫无意义。
    # 2026-10-24 真发生过：CNB 上「下载测速可用 0/354」，产物 80 个节点全是 0.0，
    # 而上面所有断言都通过了 —— 名字格式对、不重复、都在组里。
    # 「自洽」不等于「有用」，所以这里必须有一条关于数值本身的断言。
    speeds = []
    for name in names:
        match = NAME_PATTERN.match(name)
        if match:
            speeds.append(float(match.group(2)))
    if speeds and max(speeds) <= 0.0:
        problems.append(
            f"{len(speeds)} 个节点的速度全是 0.0 MB/S ⇒ 测速环节没有产出任何有效读数，"
            "这份配置不该发布（查日志里「下载测速可用 x/y」和失败原因那几行）")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="dist/clash.yaml", help="要校验的配置文件")
    args = parser.parse_args()

    path = Path(args.path)
    problems = verify(path)
    if problems:
        print(f"✗ 产物校验失败（{len(problems)} 项）")
        for item in problems:
            print("   -", item)
        return 1

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    names = [p["name"] for p in doc["proxies"]]
    speeds = sorted((float(NAME_PATTERN.match(n).group(2)) for n in names), reverse=True)
    print(f"✓ 产物校验通过：{len(names)} 个节点，最快 {speeds[0]} MB/S，"
          f"最慢 {speeds[-1]} MB/S")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
