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

from clashhunter.builder import _dedupe_by_exit, _shown_speed, _stratified  # noqa: E402
from clashhunter.mihomo import _plausible_exit_ip  # noqa: E402


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
    # 把被测函数的日志吞掉，避免刷屏
    quiet = lambda *a, **k: None  # noqa: E731

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

    # 8. 单国上限：名额少、国家多时，配额会退化成 1，剩下的名额全按速度补，
    #    而测速机在美国 ⇒ 实测取 80 个时美国占了 30 个（37.5%）。
    #    加 cap_per_country 后美国必须被压到上限，且**总数仍然补到 limit**。
    others2 = {f"C{i:02d}": 20 for i in range(46)}          # 46 个国家各 20 条
    items2, codes2 = build_corpus(500, others2)             # 美国 500 条
    # 让美国的速度全部最快（v 最小），模拟「测速机在美国」的真实偏置
    for i, it in enumerate(items2):
        if codes2[it["name"]] == "US":
            it["v"] = i
    out_cap = _stratified(items2, 80, codes2, key=lambda p: p["v"],
                          cap_per_country=16, log=quiet)
    dist_cap = Counter(codes2[p["name"]] for p in out_cap)
    if len(out_cap) != 80:
        problems.append(f"加单国上限后总数 {len(out_cap)} != 80（上限不能把名额吃掉）")
    if dist_cap.get("US", 0) > 16:
        problems.append(f"美国 {dist_cap['US']} 个，超过上限 16")
    if dist_cap.get("US", 0) < 16:
        problems.append(f"美国只有 {dist_cap['US']} 个，说明上限把美国压过头了")
    # 不加上限时美国应该远超 16，用来证明这条测试确实测到了差别
    out_nocap = _stratified(items2, 80, codes2, key=lambda p: p["v"])
    if Counter(codes2[p["name"]] for p in out_nocap)["US"] <= 16:
        problems.append("不加上限时美国也没超过 16，这条用例构造不出偏置，测不出差别")

    # 上限卡死后凑不满时要给出警告，而不是静默少数
    # 注意：候选总数 ≤ limit 时函数会原样返回（cap 不参与），所以这里必须让总数大于 limit
    thin = [{"name": f"u{i}", "v": i} for i in range(50)]
    thin_codes = {p["name"]: "US" for p in thin}
    out_thin = _stratified(thin, 40, thin_codes, key=lambda p: p["v"],
                           cap_per_country=5, log=quiet)
    if len(out_thin) != 5:
        problems.append(f"只有 50 条美国、要 40 个、上限 5 时输出 {len(out_thin)}，应为 5")
    # 候选总数不足 limit 时原样返回，上限不该把名单砍掉
    out_few = _stratified(thin, 80, thin_codes, key=lambda p: p["v"],
                          cap_per_country=5, log=quiet)
    if len(out_few) != 50:
        problems.append(f"候选只有 50 条、要 80 个时输出 {len(out_few)}，应为 50（原样返回）")


    # 9. 同一个出口 IP 只能留最快的那个
    #    背景：nodes.dedupe() 的键是 (type, server, port, 凭证)，所以「同一台服务器、
    #    同一个端口，只是凭证不同」的两条会被当成两个节点。run #7 的最终 100 个里
    #    有三对其实是同一个出口，等于白扔了 3 个名额。这里直接用那三对真实数据。
    dupes = [
        {"name": "us07", "type": "http", "server": "138.199.35.215", "port": 9002},
        {"name": "us13", "type": "http", "server": "138.199.35.215", "port": 9002},
        {"name": "cm01", "type": "http", "server": "102.244.78.61", "port": 8086},
        {"name": "cm02", "type": "http", "server": "102.244.78.61", "port": 8084},
        {"name": "noexit1", "type": "vless", "server": "43.154.165.40", "port": 443},
        {"name": "noexit2", "type": "vless", "server": "116.204.164.17", "port": 7443},
    ]
    dup_probes = {
        "us07": {"speed": 9.9, "exit_ip": "138.199.35.215"},
        "us13": {"speed": 9.6, "exit_ip": "138.199.35.215"},
        "cm01": {"speed": 0.05, "exit_ip": "102.244.78.57"},
        "cm02": {"speed": 0.05, "exit_ip": "102.244.78.57"},
        "noexit1": {"speed": 2.2, "exit_ip": None},
        "noexit2": {"speed": 2.6, "exit_ip": None},
    }
    out_du = _dedupe_by_exit(dupes, dup_probes, log=quiet)
    got_names = [p["name"] for p in out_du]
    if sorted(got_names) != ["cm01", "noexit1", "noexit2", "us07"]:
        problems.append(f"同出口合并结果 {sorted(got_names)}，"
                        f"应为 ['cm01','noexit1','noexit2','us07']")
    if len(out_du) != 4:
        problems.append(f"同出口合并后 {len(out_du)} 个，应为 4（6 个里有 2 个是同出口）")

    # 慢的排在前面时也要被快的替换掉 —— 否则「留最快」只是「留第一个」
    rev = [
        {"name": "slow", "type": "http", "server": "1.2.3.4", "port": 80},
        {"name": "fast", "type": "http", "server": "1.2.3.4", "port": 80},
    ]
    rev_probes = {
        "slow": {"speed": 1.0, "exit_ip": "9.9.9.9"},
        "fast": {"speed": 8.0, "exit_ip": "9.9.9.9"},
    }
    out_rev = _dedupe_by_exit(rev, rev_probes, log=quiet)
    if [p["name"] for p in out_rev] != ["fast"]:
        problems.append(f"同出口保留的是 {[p['name'] for p in out_rev]}，应保留更快的 'fast'")

    # 没有 exit_ip 的节点：只要 server:port 不同就都保留（无从判断是否同出口）
    if len(_dedupe_by_exit(dupes[4:], dup_probes, log=quiet)) != 2:
        problems.append("没有 exit_ip 且 server:port 不同的节点被误合并了")

    # 没有 exit_ip 但 server:port 完全相同的两条，是同一条管道，必须合并。
    # 实测 run #7 的 瑞士-01/瑞士-02 就是 116.204.164.17:7443 的两条，都没测出出口 IP。
    same_ep = [
        {"name": "ch01", "type": "vless", "server": "116.204.164.17", "port": 7443},
        {"name": "ch02", "type": "vless", "server": "116.204.164.17", "port": 7443},
    ]
    same_probes = {
        "ch01": {"speed": 3.1, "exit_ip": None},
        "ch02": {"speed": 2.64, "exit_ip": None},
    }
    out_ep = _dedupe_by_exit(same_ep, same_probes, log=quiet)
    if [p["name"] for p in out_ep] != ["ch01"]:
        problems.append(f"同 server:port 且无出口 IP 时保留的是 "
                        f"{[p['name'] for p in out_ep]}，应保留更快的 'ch01'")

    # 10. 出口 IP 必须是可信的公网地址字面量
    #    背景：run #7 的 加拿大-01 的 exit_ip 是 104.26.0.0 —— Cloudflare 网段地址，
    #    不是任何客户端的地址，GeoIP 照样给它编了个国家，于是节点名里的国家是错的。
    ip_cases = [
        ("104.26.0.0", False, "网段地址，ipify 不会把客户端报成这个"),
        ("102.244.78.255", False, "广播地址"),
        ("127.0.0.1", False, "回环"),
        ("10.0.0.5", False, "私有地址"),
        ("192.168.1.7", False, "私有地址"),
        ("<html>", False, "代理返回的错误页"),
        ("", False, "空响应"),
        ("138.199.35.215", True, "正常公网地址"),
        ("2606:4700::1111", True, "公网 IPv6"),
        ("::1", False, "回环 IPv6"),
    ]
    for text, want, why in ip_cases:
        got = _plausible_exit_ip(text)
        if got != want:
            problems.append(f"_plausible_exit_ip({text!r}) = {got}，应为 {want}（{why}）")


    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 分层取样通过：小国全部保留、大国被配额约束、边界场景正常")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
