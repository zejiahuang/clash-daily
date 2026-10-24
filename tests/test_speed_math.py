"""下载测速的「平均速度」算式回归测试。

背景：这里最容易犯的错是**把握手时间算进分母**。它不会报错 —— 表现是所有节点的读数
被系统性压低，而且速度越快压得越狠（2 MB 的下载在 10 MB/s 上只要 0.2 s，
握手 0.15 s 就能把 10 报成 5.7），于是排名被压平，而日志和报告一切正常。

所以这里用**假时钟 + 假响应**把算式钉死，不碰网络：
1. 结果必须等于「总字节 / (首字节 → 末字节)」
2. 握手多花多久都不许影响结果 ← 这条是核心
3. 下载量不足 64 KiB 视为失败
4. 极快节点由 0.05 s 分母下限兜住，不许除出天文数字
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashhunter.mihomo import Mihomo  # noqa: E402


class Clock:
    """可控时钟。measure_speed 只通过 time.time() 取时间。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = float(start)

    def time(self) -> float:
        return self.t


class FakeResp:
    def __init__(self, clock: Clock, state: dict, chunk: int, per_chunk: float) -> None:
        self.clock = clock
        self.state = state
        self.chunk = chunk
        self.per_chunk = per_chunk

    def read(self, size: int) -> bytes:
        if self.state["reads"] >= self.state["chunks"]:
            return b""
        self.clock.t += self.per_chunk
        self.state["reads"] += 1
        n = min(size, self.chunk)
        self.state["bytes"] += n
        return b"x" * n

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """模拟「连上代理 → 收到响应」。handshake 只在 open() 里消耗时间。"""

    def __init__(self, clock: Clock, *, chunks: int, chunk: int,
                 per_chunk: float, handshake: float) -> None:
        self.clock = clock
        self.chunks = chunks
        self.chunk = chunk
        self.per_chunk = per_chunk
        self.handshake = handshake
        self.state = {"reads": 0, "bytes": 0, "chunks": chunks}

    def open(self, req, timeout=None):
        self.clock.t += self.handshake
        return FakeResp(self.clock, self.state, self.chunk, self.per_chunk)


def measure(*, chunks: int, chunk: int, per_chunk: float, handshake: float) -> tuple[float, dict]:
    clock = Clock()
    opener = FakeOpener(clock, chunks=chunks, chunk=chunk,
                        per_chunk=per_chunk, handshake=handshake)
    mh = Mihomo(Path("x"), Path("."), log=lambda *a, **k: None)
    with patch.object(Mihomo, "_opener", staticmethod(lambda port: opener)), \
         patch("clashhunter.mihomo.time.time", clock.time):
        speed = mh.measure_speed(1, max_bytes=2_000_000, timeout=10.0)
    return speed, opener.state


def main() -> int:
    problems: list[str] = []

    # 1. 基本算式：总字节 / (首字节 → 末字节)
    speed, st = measure(chunks=40, chunk=65536, per_chunk=0.01, handshake=0.0)
    reads, nbytes = st["reads"], st["bytes"]
    want = nbytes / ((reads - 1) * 0.01) / 1048576
    if abs(speed - want) > 1e-6:
        problems.append(f"平均值 {speed:.4f} != 手算 {want:.4f}"
                        f"（{nbytes} B / {(reads - 1) * 0.01:.2f} s）")

    # 2. 握手时间不许进分母 —— 这一条是这次改动的全部理由
    for handshake in (0.15, 0.5, 2.0):
        slow, _ = measure(chunks=40, chunk=65536, per_chunk=0.01, handshake=handshake)
        if abs(slow - speed) > 1e-6:
            problems.append(f"握手 {handshake}s 时结果变成 {slow:.4f}，"
                            f"应仍为 {speed:.4f} —— 握手被算进分母了")

    # 3. 不足 64 KiB 视为失败
    tiny, _ = measure(chunks=40, chunk=1024, per_chunk=0.01, handshake=0.0)
    if tiny != 0.0:
        problems.append(f"只下了 40 KiB 却报出 {tiny:.4f} MB/s，应为 0")

    # 4. 极快节点由 0.05 s 下限兜住：2 MB 两三个 chunk 就收完，不能除出假数字
    fast, st_fast = measure(chunks=4, chunk=65536, per_chunk=0.0001, handshake=0.0)
    cap = st_fast["bytes"] / 0.05 / 1048576
    if fast > cap + 1e-6:
        problems.append(f"极快节点报出 {fast:.2f} MB/s，超过 0.05 s 下限允许的 {cap:.2f}")
    if fast <= 0:
        problems.append("极快节点报出 0，分母下限把结果吃掉了")

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print(f"✓ 测速算式通过：平均值 {speed:.2f} MB/s 与手算一致，"
          f"握手时间不进分母，极快/极小下载都有兜底")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
