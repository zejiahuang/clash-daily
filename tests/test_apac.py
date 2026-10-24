"""验证「经亚太跳板复测」的接线与判定。

为什么单独测：这条链路有三处错了都不报错，只会静默产出无意义的值 ——

① 漏挂 `dialer-proxy`：那个节点就是直连测的，结果混进「亚太视角」统计里，
   看起来一切正常，实际上分母里掺了假数据。
② 跳板占了监听端口：会顶掉一个被测节点的端口，那个节点测出来是 0。
③ 跳板同时出现在被测节点里：mihomo 里就是自我循环，整批启动失败。

另外还要钉住判据本身：**只有「亚太测值远低于天花板」时该测量才有分辨力**。
测出 0.8 而天花板 16 ⇒ 它从亚太看就是 0.8；测出 16 ⇒ 只能说「至少 16」。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter.builder import APAC_CODES, _apac_check  # noqa: E402
from clashhunter.mihomo import Mihomo  # noqa: E402

QUIET = lambda *a, **k: None  # noqa: E731


def node(name: str) -> dict:
    return {"name": name, "type": "vless", "server": "1.2.3.4", "port": 443}


def test_hop_wiring(problems: list[str]) -> None:
    """每个被测节点都要挂 dialer-proxy，跳板不占端口、自己也不挂。"""
    mh = Mihomo(Path("mihomo"), Path("."), log=QUIET)
    targets = [node("A"), node("B"), node("C")]
    hop = node("JP-hop")
    seen: dict = {}

    def fake_start(self, proxies, listener_ports=None, ready_port=None):
        seen["proxies"] = proxies
        seen["ports"] = listener_ports

    def fake_probe_one(self, port, *, speed_url, speed_bytes, speed_timeout, ip_timeout):
        return {"speed": 1.0, "exit_ip": None}

    with patch.object(Mihomo, "start", fake_start), \
         patch.object(Mihomo, "probe_one", fake_probe_one):
        out = mh.probe_all(targets, hop=hop, speed_bytes=1_000, log=QUIET)

    sent = {p["name"]: p for p in seen.get("proxies", [])}
    if "JP-hop" not in sent:
        problems.append("跳板没有出现在下发给 mihomo 的 proxies 里，dialer-proxy 会指向不存在的名字")
    elif "dialer-proxy" in sent["JP-hop"]:
        problems.append("跳板自己也被挂上了 dialer-proxy（会自我循环）")

    for name in ("A", "B", "C"):
        got = sent.get(name, {}).get("dialer-proxy")
        if got != "JP-hop":
            problems.append(f"节点 {name} 的 dialer-proxy 是 {got!r}，应为 'JP-hop'"
                            "（漏挂就等于直连测的，会污染亚太视角统计）")

    ports = seen.get("ports") or {}
    if "JP-hop" in ports:
        problems.append("跳板被分配了监听端口，会顶掉一个被测节点的端口")
    if sorted(ports) != ["A", "B", "C"]:
        problems.append(f"端口只应分给被测节点，实际分给了 {sorted(ports)}")
    if set(out) != {"A", "B", "C"}:
        problems.append(f"返回值应只含被测节点，实际 {sorted(out)}")


def test_hop_self_loop(problems: list[str]) -> None:
    """跳板混在被测节点里必须直接报错，不能静默跑。"""
    mh = Mihomo(Path("mihomo"), Path("."), log=QUIET)
    hop = node("JP-hop")
    try:
        mh.probe_all([hop, node("A")], hop=hop, log=QUIET)
    except ValueError:
        return
    problems.append("跳板同时出现在被测节点里却没有报错（会自我循环）")


class FakeMihomo:
    """只回答「经跳板测出多少」，并记录被要求测了谁。"""

    def __init__(self, speeds: dict[str, float]):
        self.speeds = speeds
        self.asked: list[str] = []
        self.hop: dict | None = None

    def probe_all(self, proxies, *, hop=None, **kwargs):
        self.asked = [p["name"] for p in proxies]
        self.hop = hop
        return {p["name"]: {"speed": self.speeds.get(p["name"], 0.0), "exit_ip": None}
                for p in proxies}


def test_false_positive_rule(problems: list[str]) -> None:
    """直连快、亚太侧掉到 1 以下 ⇒ 记入假阳性；撞天花板的不能算。"""
    cands = [node(n) for n in ("US-fast", "JP-hop", "DE-mid", "NL-ceil")]
    # 直连速度：US-fast 112（美西视角的明星节点）、JP-hop 16.8（最快亚太）、
    # DE-mid 20、NL-ceil 30
    probes = {
        "US-fast": {"speed": 112.0, "exit_ip": "9.9.9.9"},
        "JP-hop": {"speed": 16.8, "exit_ip": "8.8.8.8"},
        "DE-mid": {"speed": 20.0, "exit_ip": "7.7.7.7"},
        "NL-ceil": {"speed": 30.0, "exit_ip": "6.6.6.6"},
    }
    # NL-ceil 刻意选非亚太：它直连 30 > JP-hop 的 16.8，若是亚太节点就会把跳板抢走。
    code_of = {"US-fast": "US", "JP-hop": "JP", "DE-mid": "DE", "NL-ceil": "NL"}
    # 亚太侧：US-fast 掉到 0.5（假阳性）、DE-mid 正常 15、NL-ceil 撞天花板 16.5
    fake = FakeMihomo({"US-fast": 0.5, "DE-mid": 15.0, "NL-ceil": 16.5})
    lines: list[str] = []

    got = _apac_check(
        fake, cands, probes, code_of,
        top=10, workers=4, speed_url="http://x", speed_bytes=1, speed_timeout=1,
        log=lambda msg="": lines.append(msg),
    )

    if fake.hop is None or fake.hop["name"] != "JP-hop":
        problems.append(f"跳板应选亚太里最快的 JP-hop，实际选了 "
                        f"{fake.hop['name'] if fake.hop else None}")
    if "JP-hop" in fake.asked:
        problems.append("跳板自己被送去被测（会自我循环）")
    if "US-fast" not in fake.asked:
        problems.append("最快的直连节点没进复测样本")

    # 只报告不剔除：返回值保留真实测值，不能把它改成 0 或删掉
    if got.get("US-fast", {}).get("speed") != 0.5:
        problems.append("假阳性节点被就地改写了 —— 本轮约定是只报告不剔除")

    text = "\n".join(lines)
    if "疑似假阳性 1 个" not in text:
        problems.append(f"假阳性计数应为 1（US-fast），日志里没出现：\n{text}")
    if "US-fast" not in text:
        problems.append("假阳性节点名没有打印出来")
    if "DE-mid" in text.split("疑似假阳性")[-1]:
        problems.append("DE-mid（亚太 15 MB/s，正常）被误判成假阳性")
    if "NL-ceil" in text.split("疑似假阳性")[-1]:
        problems.append("NL-ceil 撞了天花板（16.5 ≈ 16.8），不该被当成假阳性")


def test_no_apac_pool(problems: list[str]) -> None:
    """池子里没有亚太节点时要安静跳过，不能抛异常。"""
    fake = FakeMihomo({})
    got = _apac_check(
        fake, [node("US")], {"US": {"speed": 10.0, "exit_ip": "9.9.9.9"}}, {"US": "US"},
        top=10, workers=4, speed_url="http://x", speed_bytes=1, speed_timeout=1,
        log=QUIET,
    )
    if got:
        problems.append("没有亚太节点却返回了结果")
    if fake.asked:
        problems.append("没有亚太跳板却发起了复测")


def test_apac_set(problems: list[str]) -> None:
    """亚太集合的边界：香港/台湾/澳门必须在内，且必须是两位码。"""
    for code in ("JP", "HK", "TW", "MO", "SG", "KR", "ID", "AU"):
        if code not in APAC_CODES:
            problems.append(f"亚太集合缺 {code}")
    for code in ("US", "DE", "GB", "FR"):
        if code in APAC_CODES:
            problems.append(f"{code} 不是亚太，不该在集合里")
    if any(len(c) != 2 or c != c.upper() for c in APAC_CODES):
        problems.append("集合里出现了非两位大写国家码（GeoIP 返回的就是两位大写）")


def main() -> int:
    problems: list[str] = []
    test_hop_wiring(problems)
    test_hop_self_loop(problems)
    test_false_positive_rule(problems)
    test_no_apac_pool(problems)
    test_apac_set(problems)
    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 亚太复测通过：dialer-proxy 挂全、跳板不占端口、自我循环被拦、"
          "假阳性判据只认「远低于天花板」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
