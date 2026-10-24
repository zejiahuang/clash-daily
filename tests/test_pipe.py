"""钉住「出口管道自检 → 定初筛参数」这条接线与它背后的算术。

为什么单独测：`_fit_to_pipe()` 是一个**纯函数**，它算错不会在设备上出声 ——
表现只是「所有节点读数偏低」或「全是 0」，和「今天节点都不行」长得一模一样。
本项目已经有过一次同类教训（`ordered()` 的键写错，整次优选静默变成空操作）。
所以这里把三件事分开钉：

  A. `_fit_to_pipe()` 的算术：管道窄到什么程度才压并发、压到多少、下载量取多少；
  B. `_measure_pipe()` 交出的必须是**并发总量**，不是单连接读数 ——
     两者在「按连接限速」的出口上差 K 倍，而 `_fit_to_pipe()` 只认前者；
  C. `build()` 里自检必须排在初筛**之前**，否则调好的参数根本没被用上。

⚠️ B 里那条「单连接 1.06 MB/s 不能解释 0/354」不是推测，是算式：
   若管道真是 1 MB/s 且被 32 并发均分，每个节点 10 秒能读到约 300 KB，
   算出 0.03 MB/s —— 会被计为**可用**。0 只出现在「没读到」或「不到 64 KB」。
"""

from __future__ import annotations

import ast
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter.builder import (  # noqa: E402
    PIPE_PROBE_BYTES,
    RETRY_BYTES,
    SOURCE_MIN_MBS,
    _fit_to_pipe,
    _measure_pipe,
)
from clashhunter.mihomo import Mihomo  # noqa: E402

QUIET = lambda *a, **k: None  # noqa: E731
ROOT = Path(__file__).resolve().parent.parent
MB = 1048576


class FakePipe:
    """只提供 measure_aggregate 的假内核，按 conns 返回预设读数。"""

    def __init__(self, by_conns: dict[int, dict]):
        self._by_conns = by_conns
        self.seen: list[int] = []

    def measure_aggregate(self, url, *, conns, max_bytes, timeout):
        self.seen.append(conns)
        return self._by_conns.get(conns, {"conns": conns, "ok": 0, "agg": 0.0,
                                          "bytes": 0, "wall_s": 0.0, "errs": []})


# --------------------------------------------------------------- A. 算术


def test_fit_narrow(problems: list[str]) -> None:
    """管道比门槛窄 ⇒ 并发压到 1，下载量降到 1 MB（不往上抬）。"""
    workers, nbytes = _fit_to_pipe(1.06, 32, 6_000_000, 10.0, log=QUIET)
    if workers != 1:
        problems.append(f"管道 1.06 MB/s 时并发应压到 1，实际 {workers}")
    if nbytes != 1048576:
        problems.append(f"管道 1.06 MB/s 时每节点下载量应是 1 MB，实际 {nbytes}"
                        f"（1 MB 给的读数上限 20 MB/s 已高于管道天花板；"
                        f"再大只按倍数拉长墙钟）")


def test_fit_never_grows(problems: list[str]) -> None:
    """`--speed-bytes` 已经比管道小的时候不能往上抬 —— 那会覆盖用户给的值。"""
    workers, nbytes = _fit_to_pipe(1.0, 32, 300_000, 10.0, log=QUIET)
    if nbytes != 300_000:
        problems.append(f"用户已给 300 KB（小于管道 1 秒的量）时下载量被改成 {nbytes}"
                        f" —— 只该往下压，不该往上抬")
    if workers != 1:
        problems.append(f"窄管道下并发仍应是 1，实际 {workers}")


def test_fit_wide(problems: list[str]) -> None:
    """管道够粗时：来得及就不动，来不及就只收并发、不改下载量。"""
    if _fit_to_pipe(100.0, 32, 6_000_000, 10.0, log=QUIET) != (32, 6_000_000):
        problems.append("管道 100 MB/s 时 32×6 MB 只要 1.9 秒，不该做任何调整")

    workers, nbytes = _fit_to_pipe(30.0, 32, 6_000_000, 10.0, log=QUIET)
    if nbytes != 6_000_000:
        problems.append(f"宽管道下不该改下载量（复测还靠它抬 0.05 秒上限），实际 {nbytes}")
    if not (1 <= workers < 32):
        problems.append(f"30 MB/s 上 32×6 MB 要 6.4 秒、超过超时的一半，"
                        f"并发应被收到中间值，实际 {workers}")


def test_fit_unknown(problems: list[str]) -> None:
    """量不出管道（0）时不能猜：原样返回，让旧参数继续跑。"""
    for args in [(0.0, 32, 6_000_000, 10.0), (-1.0, 32, 6_000_000, 10.0),
                 (1.0, 0, 6_000_000, 10.0), (1.0, 32, 0, 10.0),
                 (1.0, 32, 6_000_000, 0.0)]:
        if _fit_to_pipe(*args, log=QUIET) != (args[1], args[2]):
            problems.append(f"_fit_to_pipe{args} 应原样返回，实际 "
                            f"{_fit_to_pipe(*args, log=QUIET)}")


def test_fit_guarantees_reading(problems: list[str]) -> None:
    """压完之后，每个节点在超时内必须能读到 64 KB 门槛之上。

    这条是 `_fit_to_pipe` 存在的**唯一理由**：读数 0 的成因之一是
    「管道被均分后连 64 KB 都凑不到」，压参数就是为了让这种情况不发生。
    """
    for pipe in (0.3, 0.5, 1.0, 2.0, 5.0, 10.0, 19.9):
        workers, nbytes = _fit_to_pipe(pipe, 32, 6_000_000, 10.0, log=QUIET)
        per_conn = pipe / workers           # 每连接能分到的带宽（MB/s）
        need = (nbytes / MB) / per_conn     # 读完 nbytes 要多少秒
        if need > 10.0:
            problems.append(f"管道 {pipe} MB/s 压成 {workers} 并发 × "
                            f"{nbytes / 1e6:.1f} MB 之后仍要 {need:.1f}s，"
                            f"超过 10s 超时 ⇒ 读数还是 0")
        if nbytes < 65536:
            problems.append(f"管道 {pipe} MB/s 时每节点下载量 {nbytes} 低于 64 KB 门槛")


def test_fit_wall_time(problems: list[str]) -> None:
    """整段墙钟 ≈ `节点数 × nbytes / 管道`，与并发无关。

    这条是「中间有一段太长」的量化约束：354 个节点、管道 1 MB/s 时，
    每节点 1 MB 就是 354 秒 —— 这是地板，不是调参能绕过的；
    但如果下载量按「管道 1 秒的量」给（管道 5 MB/s 时给 5 MB），
    墙钟会白白变成 5 倍（1770 秒），而读数一点没变好。
    """
    nodes = 354
    for pipe in (1.0, 5.0, 10.0, 19.0):
        _, nbytes = _fit_to_pipe(pipe, 32, 6_000_000, 10.0, log=QUIET)
        wall = nodes * (nbytes / MB) / pipe
        if wall > 600:
            problems.append(f"管道 {pipe} MB/s 时这一阶段要 {wall:.0f}s"
                            f"（{nodes} 个节点 × {nbytes / MB:.1f} MB / {pipe}）"
                            f"，超过 10 分钟 —— 下载量应当再小")
        if nbytes != 1048576:
            problems.append(f"管道 {pipe} MB/s 时下载量应统一压到 1 MB，实际 "
                            f"{nbytes / MB:.2f} MB（墙钟 {wall:.0f}s）")


# --------------------------------------------------- B. 管道量的是并发总量


def test_measure_pipe_uses_aggregate(problems: list[str]) -> None:
    """必须用并发总量当管道，不能拿单连接读数冒充。

    单连接 1 MB/s、6 并发合计 12 MB/s（典型的按连接限速出口）时，
    若把 1 MB/s 当管道，就会把 32 并发压成 1 并发 —— 白白慢 6 倍。
    """
    fake = FakePipe({
        1: {"conns": 1, "ok": 1, "agg": 1.0, "bytes": 1 * MB, "wall_s": 6.0, "errs": []},
        6: {"conns": 6, "ok": 6, "agg": 12.0, "bytes": 12 * MB, "wall_s": 6.0, "errs": []},
    })
    pipe, single = _measure_pipe(fake, url="u", conns=6, nbytes=6_000_000,
                                 timeout=6.0, log=QUIET)
    if pipe != 12.0:
        problems.append(f"管道应取并发总量 12.0，实际 {pipe} —— 拿单连接的 1.0 当管道"
                        f"会把并发压到 1，白白慢 6 倍")
    if single != 1.0:
        problems.append(f"单连接读数应一并返回（用于判断「按连接限速」还是「出口真窄」），"
                        f"实际 {single}")
    if fake.seen != [1, 6]:
        problems.append(f"应先量单连接再量并发（{fake.seen}）—— 顺序反了就没法比"
                        f"「并发有没有提升」")


def test_measure_pipe_flat(problems: list[str]) -> None:
    """并发没有提升时，管道就是单连接那个数（出口真的窄）。"""
    fake = FakePipe({
        1: {"conns": 1, "ok": 1, "agg": 1.0, "bytes": MB, "wall_s": 6.0, "errs": []},
        6: {"conns": 6, "ok": 5, "agg": 1.1, "bytes": MB, "wall_s": 6.0, "errs": []},
    })
    pipe, _ = _measure_pipe(fake, url="u", conns=6, nbytes=6_000_000,
                            timeout=6.0, log=QUIET)
    if pipe != 1.1:
        problems.append(f"并发无提升时管道应取 1.1（实测值），实际 {pipe}")


def test_measure_pipe_dead(problems: list[str]) -> None:
    """并发自检一个字节都没读到 ⇒ 返回 0，让 `_fit_to_pipe` 原样放行。"""
    fake = FakePipe({1: {"conns": 1, "ok": 0, "agg": 0.0, "bytes": 0,
                         "wall_s": 6.0, "errs": ["URLError: timed out"]},
                     6: {"conns": 6, "ok": 0, "agg": 0.0, "bytes": 0,
                         "wall_s": 6.0, "errs": ["URLError: timed out"]}})
    pipe, single = _measure_pipe(fake, url="u", conns=6, nbytes=6_000_000,
                                 timeout=6.0, log=QUIET)
    if pipe != 0.0:
        problems.append(f"一个字节都没读到时管道应为 0（表示「不知道」），实际 {pipe}")
    if single != 0.0:
        problems.append(f"单连接也没读到时应为 0，实际 {single}")
    if _fit_to_pipe(pipe, 32, 6_000_000, 10.0, log=QUIET) != (32, 6_000_000):
        problems.append("管道未知时 _fit_to_pipe 不该改动参数")


def test_aggregate_arithmetic(problems: list[str]) -> None:
    """`measure_aggregate` 的总量分母是「最早首字节 → 最晚末字节」，不是各连接自己算再相加。"""
    mh = Mihomo(Path("mihomo"), Path("."), log=QUIET)
    rows = [(2 * MB, 100.0, 102.0, None),      # 2 MB / 2s
            (4 * MB, 101.0, 104.0, None),      # 4 MB / 3s
            (0, 0.0, 0.0, "URLError: refused")]
    with patch.object(Mihomo, "_pull", side_effect=rows):
        got = mh.measure_aggregate("u", conns=3, max_bytes=6_000_000, timeout=6.0)
    # 合计 6 MB，跨度 100.0 → 104.0 = 4s ⇒ 1.5 MB/s（各连接自己算再相加会是 1+1.33=2.33）
    if abs(got["agg"] - 1.5) > 0.01:
        problems.append(f"并发总量算出 {got['agg']}，应为 6 MB / 4s = 1.5"
                        f"（分母要跨连接取「最早首字节→最晚末字节」）")
    if got["ok"] != 2:
        problems.append(f"有数据的连接数应为 2，实际 {got['ok']}")
    if not got["errs"]:
        problems.append("失败原因必须带出来 —— 不然「全灭」只剩一个 0")


def test_speed_ex_unchanged(problems: list[str]) -> None:
    """`_pull` 抽出来之后，`measure_speed_ex` 的读数必须一字不变。"""
    mh = Mihomo(Path("mihomo"), Path("."), log=QUIET)
    with patch.object(Mihomo, "_pull",
                      side_effect=[(6 * MB, 10.0, 12.0, None)]):
        speed, err = mh.measure_speed_ex(None, "u", 6_000_000, 10.0)
    if abs(speed - 3.0) > 1e-9 or err is not None:
        problems.append(f"6 MB / 2s 应得 3.0 MB/s、无错误，实际 {speed} / {err}")

    with patch.object(Mihomo, "_pull", side_effect=[(1000, 0.0, 0.0, None)]):
        speed, err = mh.measure_speed_ex(None, "u", 6_000_000, 10.0)
    if speed != 0.0 or not err:
        problems.append(f"只读到 1000 字节时必须返回 0 并给出原因，实际 {speed} / {err}")
    elif "1000" not in err:
        problems.append(f"原因里要带出实际字节数（现在的措辞是「{err}」）")


# ------------------------------------------------- C. 顺序：自检必须在初筛前


def test_selfcheck_before_screen(problems: list[str]) -> None:
    """源码顺序断言：`_measure_pipe(` 必须出现在 `probe_all(` 之前。

    这不是风格问题。顺序反了，`_fit_to_pipe()` 算出来的参数没有任何用处 ——
    初筛已经用旧参数跑完了，而结果看上去完全正常（只是全 0 或偏低）。
    """
    src = (ROOT / "clashhunter" / "builder.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    build = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "build")
    calls: list[tuple[int, str]] = []
    for node in ast.walk(build):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("_measure_pipe", "_fit_to_pipe", "probe_all"):
                calls.append((node.lineno, name))
    calls.sort()
    names = [n for _, n in calls]
    if "_measure_pipe" not in names or "_fit_to_pipe" not in names:
        problems.append(f"build() 里没有调用自检/调参（只看到 {names}）")
        return
    first_pipe = names.index("_measure_pipe")
    first_fit = names.index("_fit_to_pipe")
    first_probe = names.index("probe_all")
    if not (first_pipe < first_fit < first_probe):
        problems.append(f"调用顺序是 {names}，应为 自检 → 调参 → 初筛。"
                        f"顺序反了，调好的参数根本没被用上")


def test_probe_budget(problems: list[str]) -> None:
    """自检的固定开销要有上界：两次测量 × 超时，不能变成新的「一段太长」。"""
    if PIPE_PROBE_BYTES > 8_000_000:
        problems.append(f"自检每次下载 {PIPE_PROBE_BYTES} 字节，"
                        f"在慢管子上会拖长墙钟（上限建议 8 MB）")
    if RETRY_BYTES < 65536:
        problems.append(f"重试下载量 {RETRY_BYTES} 低于 64 KB 门槛，"
                        f"重试本身就会返回 0，分不开「没读到」和「读得少」")
    if SOURCE_MIN_MBS <= 0:
        problems.append("SOURCE_MIN_MBS 必须为正")


def test_exit_ip_total_budget(problems: list[str]) -> None:
    """`exit_ip(timeout)` 是**整段预算**，不是每个地址各给一份。

    这条是并发被压到 1 之后才变致命的：四个地址各 6 秒 = 单节点 24 秒，
    而初筛只有 1 个 worker ⇒ 350 个节点串行 = 两个多小时。
    出口 IP 只是一行小响应，值得花的时间上限就是它自己。
    """
    mh = Mihomo(Path("mihomo"), Path("."), log=QUIET)
    timeouts: list[float] = []

    class TimeoutOpener:
        """模拟「一直读到超时」：按传入的 timeout 睡一会儿再失败。"""

        def open(self, req, timeout=None):
            timeouts.append(timeout)
            time.sleep(0.35)
            raise OSError("timed out")

    budget = 1.0
    started = time.monotonic()
    with patch.object(Mihomo, "_opener", staticmethod(lambda port: TimeoutOpener())):
        got = mh.exit_ip(20000, timeout=budget)
    elapsed = time.monotonic() - started

    if got is not None:
        problems.append(f"全部地址都失败时应返回 None，实际 {got!r}")
    if elapsed > budget * 1.3:
        problems.append(f"exit_ip 花了 {elapsed:.2f}s，预算是 {budget:.1f}s —— "
                        f"说明每个地址各领了一份预算（4 × {budget:.0f}s = 串行放大 4 倍）")
    if len(timeouts) > 3:
        problems.append(f"预算 {budget:.1f}s 内试了 {len(timeouts)} 个地址，应提前收手")
    if any(t <= 0 for t in timeouts if t is not None):
        problems.append(f"传给 open 的超时里有非正数：{timeouts}")


def test_probe_env_threshold_no_drift(problems: list[str]) -> None:
    """`probe_env.py` 的门槛必须与 `builder.SOURCE_MIN_MBS` 一致。

    那个脚本跑在 `pip install` 之前，只能抄一份常量；抄错了不会报错，
    只会让「报告里的结论」和「流水线实际怎么做」对不上 —— 而人只会读报告。
    """
    text = (ROOT / "scripts" / "probe_env.py").read_text(encoding="utf-8")
    match = re.search(r"^PIPE_MIN_MBS\s*=\s*([0-9.]+)", text, re.M)
    if not match:
        problems.append("probe_env.py 里找不到 PIPE_MIN_MBS")
        return
    got = float(match.group(1))
    if abs(got - SOURCE_MIN_MBS) > 1e-9:
        problems.append(f"probe_env.PIPE_MIN_MBS={got} 与 builder.SOURCE_MIN_MBS="
                        f"{SOURCE_MIN_MBS} 不一致 —— 报告结论会和流水线实际行为相反")


def test_top_reason(problems: list[str]) -> None:
    """进度行要能直接说出「多数失败是什么」，且不影响正常进度。"""
    from clashhunter.mihomo import _top_reason

    results = {
        "a": {"speed": 0.0, "speed_err": "URLError: [Errno 111] Connection refused"},
        "b": {"speed": 0.0, "speed_err": "URLError: [Errno 111] Connection refused"},
        "c": {"speed": 0.0, "speed_err": "只读到 0 字节（不足 64 KB）"},
        "d": {"speed": 2.0, "speed_err": None},
    }
    got = _top_reason(results)
    if "2 个" not in got or "Connection refused" not in got:
        problems.append(f"_top_reason 取错了最常见的失败原因：{got!r}"
                        f"（应为「2 个 ...Connection refused」）")
    if _top_reason({}) != "":
        problems.append("没有任何失败时 _top_reason 应返回空串，"
                        "否则正常进度行会被塞一句空话")
    if _top_reason({"x": {"speed": 0.0}}) != "":
        problems.append("只有 speed=0 但没有 speed_err 时应返回空串")
    long = _top_reason({"x": {"speed_err": "e" * 500}}, limit=20)
    if len(long) > 30:
        problems.append(f"_top_reason 没截断长原因（{len(long)} 字符），"
                        f"进度行会被一条长错误撑满")


def main() -> int:
    problems: list[str] = []
    test_fit_narrow(problems)
    test_fit_never_grows(problems)
    test_fit_wide(problems)
    test_fit_unknown(problems)
    test_fit_guarantees_reading(problems)
    test_fit_wall_time(problems)
    test_measure_pipe_uses_aggregate(problems)
    test_measure_pipe_flat(problems)
    test_measure_pipe_dead(problems)
    test_aggregate_arithmetic(problems)
    test_speed_ex_unchanged(problems)
    test_selfcheck_before_screen(problems)
    test_exit_ip_total_budget(problems)
    test_probe_budget(problems)
    test_probe_env_threshold_no_drift(problems)
    test_top_reason(problems)
    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 管道自检接线正确：并发总量当管道（不是单连接读数）、窄管道压到 1 并发、"
          "宽管道只收并发、自检排在初筛之前、压完保证能读到 64 KB 之上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
