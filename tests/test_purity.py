"""出口 IP 纯净度的回归测试（不联网）。

这块缺陷的形态很特别：**它不会报错，只会给出一个看起来正常的数字**。
`score_of` 是纯数据变换，写错一个符号的后果是「机房 IP 显示 95 分」——
使用者照着这个分去挑节点，而流水线全程绿着。

所以这里把每一种标记组合都过一遍，并且钉住三件事：
  1. 三个标记各自扣多少分（改常量必须同步这里）；
  2. 欺诈分是「取更严的那个」，不是「相加」也不是「覆盖」；
  3. 拿不到数据时 `known=False`、分数是 `None`，**不是 100**。
     最后这条是重点：`·净100` 和「没查到」在产物里长得一样，
     而前者会让人以为这个节点很干净。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter import builder, purity  # noqa: E402

QUIET = lambda *a, **k: None  # noqa: E731


def _flags(**kwargs) -> dict:
    base = {"proxy": False, "hosting": False, "mobile": False, "isp": "", "org": ""}
    base.update(kwargs)
    return base


def test_score_matrix(problems: list[str]) -> None:
    """每个标记单独/组合出现时的分数。"""
    cases = [
        (_flags(), None, 100),
        (_flags(hosting=True), None, 80),
        (_flags(proxy=True), None, 75),
        (_flags(mobile=True), None, 95),
        (_flags(proxy=True, hosting=True), None, 55),
        (_flags(proxy=True, hosting=True, mobile=True), None, 50),
        # 欺诈分：取 min(静态分, 100-欺诈)，不是相加
        (_flags(), 0, 100),
        (_flags(), 30, 70),
        (_flags(), 100, 0),
        (_flags(hosting=True), 10, 80),      # 90 > 80 ⇒ 静态分更低，取 80
        (_flags(hosting=True), 40, 60),      # 60 < 80 ⇒ 欺诈分更低，取 60
        (_flags(proxy=True), 200, 0),        # 越界要夹住
        (_flags(proxy=True), -5, 75),        # 负数当 0，不能加分
    ]
    for flags, fraud, want in cases:
        got = purity.score_of(flags, fraud)
        if got != want:
            problems.append(
                f"score_of({ {k: v for k, v in flags.items() if v is True} }, fraud={fraud})"
                f" = {got}，应为 {want}"
            )


def test_kind_priority(problems: list[str]) -> None:
    cases = [
        (_flags(proxy=True, hosting=True, mobile=True), "代理"),
        (_flags(hosting=True, mobile=True), "机房"),
        (_flags(mobile=True), "移动"),
        (_flags(isp="China Telecom"), "ISP"),
        (_flags(), "未知"),
    ]
    for flags, want in cases:
        got = purity.kind_of(flags)
        if got != want:
            problems.append(f"kind_of({flags}) = {got}，应为 {want}")


def test_unknown_is_not_perfect(problems: list[str]) -> None:
    """没拿到数据时必须是「未知」，不能是 100 分。"""
    info = purity.describe({}, fraud=None)
    if info["known"]:
        problems.append("describe({}) 把「没数据」标成了 known=True")
    if info["score"] is not None:
        problems.append(f"describe(空) 的 score 应为 None，实得 {info['score']}")
    if info["kind"] != "未知":
        problems.append(f"describe({{}}) 的 kind 应为「未知」，实得 {info['kind']}")
    # 有数据但全 false 时才是真的 100
    known = purity.describe(_flags(isp="x"), fraud=None)
    if not known["known"] or known["score"] != 100:
        problems.append(f"有数据且无标记时应是 known=True / 100 分，实得 {known}")


def test_fraud_fails_open(problems: list[str]) -> None:
    """Scamalytics 打不开时必须安静返回空字典 —— 它不该挡住出产物。"""
    saved = purity.fetch

    def boom(url, **kwargs):
        raise purity.FetchError("blocked")

    purity.fetch = boom
    try:
        started = time.monotonic()
        got = purity.fraud_scores(["1.2.3.4", "5.6.7.8"], log=QUIET)
        spent = time.monotonic() - started
    finally:
        purity.fetch = saved
    if got:
        problems.append(f"抓取全失败时 fraud_scores 应返回 {{}}，实得 {got}")
    if spent > 5:
        problems.append(f"抓取全失败时不该重试（花了 {spent:.1f}s）")

    # 页面上没有 Fraud Score 字段（改版）⇒ 也是空，不是 0 分
    saved = purity.fetch
    purity.fetch = lambda url, **k: "<html>改版了</html>"
    try:
        got2 = purity.fraud_scores(["1.2.3.4"], log=QUIET)
    finally:
        purity.fetch = saved
    if got2:
        problems.append(f"页面上没有 Fraud Score 时不该编出分数，实得 {got2}")


def test_finalize_name_and_record(problems: list[str]) -> None:
    """把纯净度接进 `_finalize`：分数进节点名、明细进 records。"""
    candidates = [
        {"name": "a", "type": "ss", "server": "1.1.1.1", "port": 443},
        {"name": "b", "type": "ss", "server": "2.2.2.2", "port": 443},
        {"name": "c", "type": "ss", "server": "3.3.3.3", "port": 443},
    ]
    probes = {
        "a": {"speed": 10.0, "exit_ip": "1.2.3.4"},
        "b": {"speed": 5.0, "exit_ip": "5.6.7.8"},
        "c": {"speed": 1.0, "exit_ip": "9.9.9.9"},   # ip-api 查不到这个
    }
    geo = {
        "1.2.3.4": {"code": "US", "country": "United States", "proxy": False,
                    "hosting": True, "mobile": False, "isp": "X", "org": "X", "as": "AS1"},
        "5.6.7.8": {"code": "JP", "country": "Japan", "proxy": False,
                    "hosting": False, "mobile": False, "isp": "Y", "org": "Y", "as": "AS2"},
    }

    saved_geo = builder.geoip.lookup_batch
    saved_fraud = builder.purity_mod.fraud_scores
    builder.geoip.lookup_batch = lambda ips, log=print: {k: v for k, v in geo.items()
                                                        if k in set(ips)}
    builder.purity_mod.fraud_scores = lambda ips, log=print: {"1.2.3.4": 30}
    try:
        result = builder._finalize(
            builder.BuildResult(), [dict(p) for p in candidates], probes,
            delays={}, pre_codes={}, max_nodes=10, country_share=0,
            top_per_country=0, started=time.time(), fraud_top=10, log=QUIET,
        )
    finally:
        builder.geoip.lookup_batch = saved_geo
        builder.purity_mod.fraud_scores = saved_fraud

    by_original = {r["original"]: r for r in result.records}
    if len(by_original) != 3:
        problems.append(f"应该有 3 条记录，实得 {len(by_original)}")
        return

    # a：机房 80 分，欺诈 30 ⇒ min(80, 70) = 70
    a = by_original["a"]
    if a["purity"]["score"] != 70 or a["purity"]["kind"] != "机房":
        problems.append(f"a 的纯净度应为 机房/70，实得 {a['purity']}")
    if "·净70" not in a["name"]:
        problems.append(f"a 的节点名应带 ·净70，实得 {a['name']!r}")
    # b：无标记 + isp ⇒ 100
    b = by_original["b"]
    if b["purity"]["score"] != 100 or "·净100" not in b["name"]:
        problems.append(f"b 应为 100 分且名字带 ·净100，实得 {b['name']!r} / {b['purity']}")
    # c：查不到 ⇒ 不写后缀，分数 None
    c = by_original["c"]
    if c["purity"]["known"] or c["purity"]["score"] is not None:
        problems.append(f"c 查不到 IP 时应 known=False / score=None，实得 {c['purity']}")
    if "净" in c["name"]:
        problems.append(f"c 没拿到纯净度却写进了名字：{c['name']!r}")

    if result.purity.get("known") != 2:
        problems.append(f"汇总应有 2 个已知，实得 {result.purity}")

    # 名字格式仍然是「地区-序号-速度MB/S·净N」，前缀不能被纯净度挤掉
    for record in result.records:
        head = record["name"].split("·")[0]
        if "MB/S" not in head:
            problems.append(f"节点名前缀丢了速度：{record['name']!r}")


def test_summary(problems: list[str]) -> None:
    by_name = {
        "x": purity.describe(_flags(hosting=True), fraud=10),
        "y": purity.describe(_flags(proxy=True), fraud=None),
        "z": purity.describe({}, fraud=None),
    }
    got = purity.summary(by_name)
    if got["known"] != 2 or got["unknown"] != 1:
        problems.append(f"summary 的已知/未知不对：{got}")
    if got["kinds"].get("机房") != 1 or got["kinds"].get("代理") != 1:
        problems.append(f"summary 的类型分布不对：{got['kinds']}")
    if got["with_fraud"] != 1:
        problems.append(f"summary 的 with_fraud 应为 1，实得 {got['with_fraud']}")


def main() -> int:
    problems: list[str] = []
    test_score_matrix(problems)
    test_kind_priority(problems)
    test_unknown_is_not_perfect(problems)
    test_fraud_fails_open(problems)
    test_finalize_name_and_record(problems)
    test_summary(problems)

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 纯净度通过：13 组分数组合、类型优先级、未知≠满分、抓取失败不阻塞、"
          "名字后缀与 records 明细、汇总")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
