"""同 IP 端口上限的回归测试。

这个限制不是为了「省时间」，是为了不把整台机器送进云厂商的滥用检测：
实测一轮 TCP 预筛里，某个 IP 被探了 129 个不同端口，InstaBox 直接判定为
端口扫描并把机器锁了 24 小时。上限被误删/改回 0 的代价非常大，所以钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clashhunter.builder import _cap_ports_per_server  # noqa: E402


def main() -> int:
    problems: list[str] = []

    proxies = [{"server": "1.1.1.1", "port": p}
               for p in (443, 80, 8080, 8443, 2052, 2053, 2082)]
    proxies.append({"server": "2.2.2.2", "port": 443})
    # 同一个 (server, port) 的第二个节点要保留：它虽然端口重复，但是另一条节点
    proxies.append({"server": "1.1.1.1", "port": 443})

    kept = _cap_ports_per_server(proxies, 3, log=lambda *a, **k: None)
    kept_ports = [p["port"] for p in kept if p["server"] == "1.1.1.1"]
    if kept_ports != [443, 80, 8080, 443]:
        problems.append(f"1.1.1.1 应只保留前 3 个不同端口 + 重复的 443，实得 {kept_ports}")
    if {"server": "2.2.2.2", "port": 443} not in kept:
        problems.append("别的 IP 不该被上一个 IP 的端口数影响")
    if len(kept) != 5:
        problems.append(f"应保留 5 个节点，实得 {len(kept)}")

    # 0 = 明确关掉限制（旧行为），必须原样返回
    if len(_cap_ports_per_server(proxies, 0, log=lambda *a, **k: None)) != len(proxies):
        problems.append("limit=0 时应原样返回，不能悄悄截断")

    # 端口不是整数（异常数据）时保留，交给后面的解析/测速去判
    weird = [{"server": "3.3.3.3", "port": "not-a-port"}]
    if _cap_ports_per_server(weird, 1, log=lambda *a, **k: None) != weird:
        problems.append("端口无法解析时不该丢节点")

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 同 IP 端口上限通过：跨 IP 互不影响、重复端口保留、0=关闭、脏数据不丢")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
