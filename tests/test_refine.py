"""验证「复测用更大的下载量」这条接线与它背后的算式。

为什么单独测：这个缺陷已经在本项目出现过两次同类形态 ——
**一个参数在 A 处改对了、在 B 处还是旧值，而且跑起来不报错。**

① `SPEED_URL` 里硬编码了 `?bytes=2000000`，`--speed-bytes` 改成 6000000 之后
   服务端仍然在 2 MB 处 EOF ⇒ 「改成 6 MB」是个静默的空操作。
② 复测那一轮如果沿用初筛的 `speed_bytes`，初筛用小下载量是对的，
   但复测也会被 0.05 秒地板截断 ⇒ 快节点继续算成同一个数。

所以这里钉三件事：
  A. 复测传给 `probe_all` 的 `speed_bytes` 必须是复测量，不是初筛量；
  B. 复测失败（返回 0）不能把初筛的有效结果冲掉；
  C. `build()` 与 `main.py` 的默认值不能漂移，且复测量必须大于初筛量。
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter import mihomo as mihomo_mod  # noqa: E402
from clashhunter.builder import _pick_sources, _refine_speed, build  # noqa: E402
from clashhunter.mihomo import Mihomo, _median  # noqa: E402

QUIET = lambda *a, **k: None  # noqa: E731
ROOT = Path(__file__).resolve().parent.parent

SCREEN_BYTES = 6_000_000
REFINE_BYTES = 24_000_000
FLOOR = 0.05  # measure_speed() 里 span 的下限


def node(name: str) -> dict:
    return {"name": name, "type": "vless", "server": "1.2.3.4", "port": 443}


class FakeMihomo:
    """只记录 probe_all 收到了什么，并按预设返回速度。"""

    def __init__(self, speeds: dict[str, float], record: list[dict]):
        self._speeds = speeds
        self._record = record

    def probe_all(self, proxies, **kwargs):
        names = [p["name"] for p in proxies]
        self._record.append({"names": names, **kwargs})
        return {
            n: {"speed": self._speeds[n], "exit_ip": None}
            for n in names
            if n in self._speeds
        }


def ceiling(nbytes: int) -> float:
    """下载量固定时，`max(span, 0.05)` 允许算出来的最高读数（MB/s）。"""
    return (nbytes / 1048576) / FLOOR


def test_refine_uses_own_bytes(problems: list[str]) -> None:
    """复测必须用自己的下载量，且比初筛大。"""
    nodes = [node("A"), node("B")]
    probes = {"A": {"speed": 10.0}, "B": {"speed": 5.0}}
    record: list[dict] = []
    mh = FakeMihomo({"A": 20.0, "B": 0.0}, record)

    _refine_speed(
        mh, nodes, probes,
        top=300, workers=16, speed_url="http://example/x",
        speed_bytes=REFINE_BYTES, speed_timeout=10.0, ip_timeout=6.0,
        screen_bytes=SCREEN_BYTES, log=QUIET,
    )

    if len(record) != 1:
        problems.append(f"应只调用一次 probe_all，实际 {len(record)} 次")
        return

    got = record[0].get("speed_bytes")
    if got != REFINE_BYTES:
        problems.append(
            f"复测传给 probe_all 的 speed_bytes 是 {got}，应为 {REFINE_BYTES}。"
            f"沿用初筛的 {SCREEN_BYTES} 会让快节点继续压在 0.05 秒地板上"
            f"（上限只有 {ceiling(SCREEN_BYTES):.0f} MB/s）")

    # 复测是「低并发 + 大下载量」，两个都不能退化成初筛的参数。
    if record[0].get("workers") != 16:
        problems.append(f"复测并发是 {record[0].get('workers')}，应为 16（低于初筛）")


def test_failed_refine_keeps_old(problems: list[str]) -> None:
    """复测返回 0 时不能冲掉初筛的有效值；更快时才覆盖。"""
    nodes = [node("fast"), node("flaky")]
    probes = {"fast": {"speed": 10.0}, "flaky": {"speed": 5.0}}
    record: list[dict] = []
    mh = FakeMihomo({"fast": 20.0, "flaky": 0.0}, record)

    improved = _refine_speed(
        mh, nodes, probes,
        top=300, workers=16, speed_url="http://example/x",
        speed_bytes=REFINE_BYTES, speed_timeout=10.0, ip_timeout=6.0,
        screen_bytes=SCREEN_BYTES, log=QUIET,
    )

    if probes["fast"]["speed"] != 20.0:
        problems.append(f"复测测出更快（20.0）时没有覆盖初筛值，实际 "
                        f"{probes['fast']['speed']}")
    if probes["flaky"]["speed"] != 5.0:
        problems.append(f"复测返回 0 时把初筛的有效值冲掉了（变成 "
                        f"{probes['flaky']['speed']}）—— 复测失败不能算作「节点变慢了」")
    if improved != 1:
        problems.append(f"「比初筛更快」的计数是 {improved}，应为 1")


def test_skip_conditions(problems: list[str]) -> None:
    """top<=0、或没有可复测的项时，必须完全不发起第二轮。"""
    record: list[dict] = []
    mh = FakeMihomo({"A": 1.0}, record)

    if _refine_speed(mh, [node("A")], {"A": {"speed": 1.0}}, top=0, workers=16,
                     speed_url="u", speed_bytes=REFINE_BYTES, speed_timeout=10.0,
                     ip_timeout=6.0, log=QUIET) is not None:
        problems.append("top=0 时应返回 None（表示「没跑」），否则上层会误报复测结果")

    if _refine_speed(mh, [node("A")], {"A": {"speed": 0.0}}, top=10, workers=16,
                     speed_url="u", speed_bytes=REFINE_BYTES, speed_timeout=10.0,
                     ip_timeout=6.0, log=QUIET) is not None:
        problems.append("初筛全是 0 时应返回 None，不该拿 0 分节点去复测")

    if record:
        problems.append(f"跳过条件成立时仍调用了 probe_all（{len(record)} 次）")


def test_floor_arithmetic(problems: list[str]) -> None:
    """把「地板」这回事钉成数字：6 MB 的上限落在免费节点能到达的区间里。"""
    screen_ceiling = ceiling(SCREEN_BYTES)
    refine_ceiling = ceiling(REFINE_BYTES)

    # run #11 头名 112.75，反推 span = 5.722/112.75 = 0.0507 秒 ⇒ 就是被 0.05 截断的。
    span = (SCREEN_BYTES / 1048576) / 112.75
    if span > FLOOR * 1.05:
        problems.append(f"112.75 MB/s 反推 span = {span:.4f}s，应紧贴 {FLOOR}s 地板")
    if screen_ceiling < 110:
        problems.append(f"6 MB 的上限算出 {screen_ceiling:.1f}，与实测 112.75 对不上")

    # 24 MB 的上限要远高于任何免费节点能跑出的值，地板才真正退出。
    if refine_ceiling < 400:
        problems.append(f"24 MB 的上限只有 {refine_ceiling:.1f} MB/s，"
                        f"快节点仍可能被截断")
    ratio = refine_ceiling / screen_ceiling
    if abs(ratio - REFINE_BYTES / SCREEN_BYTES) > 0.01:
        problems.append(f"上限之比 {ratio:.2f} 应等于下载量之比 "
                        f"{REFINE_BYTES / SCREEN_BYTES:.2f}")


class FakeSources:
    """只提供 measure_sources 的假内核。"""

    def __init__(self, rows: list[dict], record: list[int]):
        self._rows = rows
        self._record = record

    def measure_sources(self, *, max_bytes, timeout, log=None):
        self._record.append(max_bytes)
        return sorted(self._rows, key=lambda r: -r["speed"])


def test_median(problems: list[str]) -> None:
    """中位数的三种情形。偶数个时取中间两个的平均。"""
    cases = [
        ([], 0.0, "空"),
        ([7.0], 7.0, "单值"),
        ([1.0, 9.0, 5.0], 5.0, "奇数取中间"),
        ([1.0, 3.0], 2.0, "偶数取均值"),
        ([1.0, 2.0, 3.0, 100.0], 2.5, "被离群值拽不动"),
    ]
    for values, want, note in cases:
        got = _median(values)
        if abs(got - want) > 1e-9:
            problems.append(f"中位数 {note}：{values} → {got}，应为 {want}")


def test_pick_sources(problems: list[str]) -> None:
    """源自检的四条分支：按比例剔除 / 全低时留最快 / 全灭 / want=0。"""
    rows = [
        {"label": "cf", "url": "u-cf", "speed": 90.0},
        {"label": "ovh", "url": "u-ovh", "speed": 40.0},
        {"label": "slow", "url": "u-slow", "speed": 15.0},
        {"label": "dead", "url": "u-dead", "speed": 0.0},
    ]
    rec: list[int] = []
    got = _pick_sources(FakeSources(rows, rec), nbytes=REFINE_BYTES,
                        timeout=10.0, want=3, min_ratio=0.4, log=QUIET)
    # 门槛 = max(20, 90*0.4=36) ⇒ cf(90) 与 ovh(40) 过，slow(15) 与 dead(0) 不过
    if [label for label, _ in got] != ["cf", "ovh"]:
        problems.append(f"源自检结果 {[l for l, _ in got]}，应为 ['cf', 'ovh']"
                        f"（门槛 max(20, 90×0.4=36)，15 和 0 都该被剔除）")
    if rec and rec[0] != REFINE_BYTES:
        problems.append(f"自检用的下载量是 {rec[0]}，应等于复测量 {REFINE_BYTES}"
                        "（用地板算上限，字节数不同结论就不同）")

    # want 限制数量
    got2 = _pick_sources(FakeSources(rows, []), nbytes=REFINE_BYTES,
                         timeout=10.0, want=1, min_ratio=0.4, log=QUIET)
    if [label for label, _ in got2] != ["cf"]:
        problems.append(f"want=1 时应只留最快的 cf，实际 {[l for l, _ in got2]}")

    # 全部低于门槛 ⇒ 至少留最快的那个，不能返回空
    low = [{"label": "a", "url": "ua", "speed": 12.0},
           {"label": "b", "url": "ub", "speed": 9.0}]
    got3 = _pick_sources(FakeSources(low, []), nbytes=REFINE_BYTES,
                         timeout=10.0, want=3, min_ratio=0.4, log=QUIET)
    if [label for label, _ in got3] != ["a"]:
        problems.append(f"所有源都低于门槛时应留最快的 a，实际 "
                        f"{[l for l, _ in got3]}（返回空会退回单源，反而更差）")

    # 全部不可用 ⇒ 空列表，退回单一 speed_url
    dead = [{"label": "x", "url": "ux", "speed": 0.0}]
    if _pick_sources(FakeSources(dead, []), nbytes=REFINE_BYTES,
                     timeout=10.0, want=3, min_ratio=0.4, log=QUIET):
        problems.append("全部源不可用时应返回空列表（退回单源）")

    # want=0 ⇒ 不发起自检
    rec2: list[int] = []
    if _pick_sources(FakeSources(rows, rec2), nbytes=REFINE_BYTES,
                     timeout=10.0, want=0, min_ratio=0.4, log=QUIET):
        problems.append("want=0 时应返回空列表")
    if rec2:
        problems.append("want=0 时不应发起源自检（白花流量）")


def test_refine_passes_sources(problems: list[str]) -> None:
    """多源必须真的传到 probe_all，否则「多个网站各自说话」是假的。"""
    nodes = [node("A")]
    probes = {"A": {"speed": 10.0}}
    record: list[dict] = []
    mh = FakeMihomo({"A": 20.0}, record)
    picked = [("cf", "u-cf"), ("ovh", "u-ovh")]

    _refine_speed(
        mh, nodes, probes,
        top=300, workers=16, speed_url="http://example/x",
        speed_bytes=REFINE_BYTES, speed_timeout=10.0, ip_timeout=6.0,
        screen_bytes=SCREEN_BYTES, sources=picked, log=QUIET,
    )

    got = record[0].get("sources") if record else None
    if got != picked:
        problems.append(f"复测传给 probe_all 的 sources 是 {got!r}，应为 {picked!r}"
                        "（没传下去就还是单源，中位数无从谈起）")


class FakeResp:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FakeOpener:
    def __init__(self, resp, tag: str, used: dict):
        self._resp, self._tag, self._used = resp, tag, used

    def open(self, _req, timeout=None):
        self._used["tag"] = self._tag
        return self._resp


def test_direct_measure(problems: list[str]) -> None:
    """port=None 必须走直连 opener —— 否则「源自检」量到的是代理后的速度。"""
    used: dict = {}
    mh = Mihomo(Path("mihomo"), Path("."), log=QUIET)

    def resp():
        return FakeResp([b"x" * 65536] * 4 + [b""])

    with patch.object(Mihomo, "_opener",
                      lambda _self, _port: FakeOpener(resp(), "proxy", used)), \
         patch.object(mihomo_mod, "_direct_opener",
                      lambda: FakeOpener(resp(), "direct", used)):
        mh.measure_speed(None, "http://example/x", 200_000, 5.0)
        if used.get("tag") != "direct":
            problems.append(f"port=None 时用的是 {used.get('tag')} opener，应为 direct")
        mh.measure_speed(20000, "http://example/x", 200_000, 5.0)
        if used.get("tag") != "proxy":
            problems.append(f"给了 port 时用的是 {used.get('tag')} opener，应为 proxy")

    # 直连 opener 必须显式清空代理，否则会继承 HTTP_PROXY 环境变量。
    # ⚠️ 判据不能用「handlers 里有没有 ProxyHandler」：代理表为空时 ProxyHandler
    # 注册不到任何方法，OpenerDirector.add_handler 不会把它放进 handlers。
    # 真正的判据是「设了 HTTP_PROXY 之后，它还会不会拿这个代理去发请求」。
    with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9",
                                 "HTTPS_PROXY": "http://127.0.0.1:9",
                                 "http_proxy": "http://127.0.0.1:9",
                                 "https_proxy": "http://127.0.0.1:9"}):
        opener = mihomo_mod._direct_opener()
    proxied = [h.proxies for h in opener.handlers
               if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    if proxied:
        problems.append(f"_direct_opener 继承了环境变量里的代理：{proxied}"
                        "（源自检会变成「经代理自检」，量到的不是源站容量）")


def _argparse_defaults(path: Path) -> dict[str, object]:
    """从 main.py 源码里抽 `add_argument("--x", ..., default=...)`，避免 import 触发副作用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "add_argument"):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if not flags or not isinstance(flags[0], str):
            continue
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                found[flags[0]] = kw.value.value
    return found


def test_defaults_no_drift(problems: list[str]) -> None:
    """build() 与 main.py 的默认值必须一致，且复测量 > 初筛量。"""
    sig = inspect.signature(build)
    b_screen = sig.parameters["speed_bytes"].default
    b_refine = sig.parameters["refine_bytes"].default

    cli = _argparse_defaults(ROOT / "main.py")
    m_screen = cli.get("--speed-bytes")
    m_refine = cli.get("--refine-bytes")

    if m_refine is None:
        problems.append("main.py 里没有 --refine-bytes（复测量只能靠代码默认值，"
                        "CI 无法调整）")
    elif m_refine != b_refine:
        problems.append(f"--refine-bytes 的默认值 {m_refine} 与 build() 的 "
                        f"{b_refine} 不一致 —— 两处任一改动都会让另一处静默失效")

    if m_screen != b_screen:
        problems.append(f"--speed-bytes 的默认值 {m_screen} 与 build() 的 {b_screen} 不一致")

    if b_refine <= b_screen:
        problems.append(f"refine_bytes({b_refine}) 必须大于 speed_bytes({b_screen})，"
                        f"否则复测等于没做")

    m_src = cli.get("--speed-sources")
    b_src = sig.parameters["speed_sources"].default
    if m_src is None:
        problems.append("main.py 里没有 --speed-sources（多源只能在代码里改）")
    elif m_src != b_src:
        problems.append(f"--speed-sources 默认值 {m_src} 与 build() 的 {b_src} 不一致")


def main() -> int:
    problems: list[str] = []
    test_refine_uses_own_bytes(problems)
    test_failed_refine_keeps_old(problems)
    test_skip_conditions(problems)
    test_floor_arithmetic(problems)
    test_median(problems)
    test_pick_sources(problems)
    test_refine_passes_sources(problems)
    test_direct_measure(problems)
    test_defaults_no_drift(problems)
    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print(f"✓ 复测接线正确：{SCREEN_BYTES // 1000000} MB（上限 "
          f"{ceiling(SCREEN_BYTES):.0f} MB/s）→ {REFINE_BYTES // 1000000} MB（上限 "
          f"{ceiling(REFINE_BYTES):.0f} MB/s）；多源取中位数已接通，"
          f"源自检按比例剔除、失败不冲掉初筛值、默认值无漂移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
