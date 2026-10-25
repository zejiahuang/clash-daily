"""GitHub Actions 侧的下载测速：读 CNB 交来的候选，**经节点**用 Cloudflare 测速，写回结果。

这个模块只做一件事，因为它的运行位置很特殊：它跑在 GitHub runner 上，
而 GitHub runner 的出口和 CNB 完全不同（宽得多，但在地理上离国内用户更远）。
所以这里的每一条规则都是为了「只测节点，不测这台机器」：

1. **不量 runner 自己的管道。** `builder._measure_pipe()` 用 `port=None` 直连，
   量的是「这台机器的出口有多宽」—— 那是 `all` 段（本机一条龙）才需要的参数，
   在这里没有意义：GitHub runner 的管道足够宽，初筛用默认并发即可，
   而任何直连读数都会被误当成「节点速度」的一部分。判据：本文件里
   **没有** `measure_aggregate` / `measure_sources` / `measure_speed(None, ...)`，
   `tests/test_handoff.py` 有一条断言盯着这件事。

2. **只用 Cloudflare 一个源。** `--speed-sources > 1` 的多源取中位数要先直连
   自检挑源（`_pick_sources` 也是直连），这里不做；`SPEED_URL` 本身就是
   `speed.cloudflare.com`，符合「用 Cloudflare 测速」。

3. **结果必须带 `run_id`。** 回传文件是提交进仓库的，上一轮那份会一直在。
   CNB 侧靠 `run_id` 判断「这份结果是不是本轮的」，见 `builder._load_speed()`。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .builder import _reason_counts, _refine_speed, _report_refine
from .mihomo import SPEED_URL, Mihomo, ensure_binary

CST = timezone(timedelta(hours=8))
UA = "clash-daily-speedtest/1.0"

# GitHub runner 的出口宽得多（Azure，常见 200+ MB/s），初筛沿用默认值即可。
# ⚠️ 这两个数是「上限」，不是承诺：真正的天花板是 runner 的出口，
#    而这里**故意不量**它（见模块 docstring 第 1 条）。
SCREEN_BYTES = 6_000_000
SCREEN_WORKERS = 32
SCREEN_TIMEOUT = 10.0
REFINE_BYTES = 24_000_000
REFINE_WORKERS = 16
REFINE_TOP = 300


def _vantage(timeout: float = 8.0) -> str:
    """GitHub runner 的地理位置，只用于**标注**速度是在哪里测的。

    这不是测速：它只发一次几十字节的 GET，问「我是谁」。
    但没有它，产物头部就会写着 CNB 的位置，而速度其实是 GitHub 测的 ——
    使用者会把「美西→节点」的速度当成「国内→节点」的速度。
    """
    try:
        with urllib.request.urlopen(
            urllib.request.Request("http://ip-api.com/json/?fields=status,country,"
                                   "countryCode,city,isp",
                                   headers={"User-Agent": UA}),
            timeout=timeout,
        ) as resp:
            geo = json.loads(resp.read(4096).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    if geo.get("status") != "success":
        return ""
    from . import countries as country_mod

    code = str(geo.get("countryCode") or "").upper()
    name = (country_mod.zh_name(code) if code else "") or str(geo.get("country") or "")
    city = str(geo.get("city") or "")
    return "·".join(part for part in (name, city) if part)


def run(in_path: Path, out_path: Path, *, work_dir: Path,
        screen_bytes: int = SCREEN_BYTES, workers: int = SCREEN_WORKERS,
        timeout: float = SCREEN_TIMEOUT, refine_top: int = REFINE_TOP,
        refine_workers: int = REFINE_WORKERS, refine_bytes: int = REFINE_BYTES,
        speed_url: str = SPEED_URL, ip_timeout: float = 6.0,
        log=print) -> dict:
    """跑完一整轮下载测速，返回写进 `out_path` 的那份 dict。"""
    started = time.time()
    payload = json.loads(Path(in_path).read_text(encoding="utf-8"))
    candidates = [dict(item) for item in payload.get("candidates") or []]
    run_id = str(payload.get("run_id") or "")
    if not candidates:
        raise SystemExit(f"{in_path} 里没有候选节点")
    if not run_id:
        raise SystemExit(f"{in_path} 缺 run_id —— 回传结果时无法与本轮配对")

    log(f"候选 {len(candidates)} 个（来自 CNB，run_id={run_id}）")
    # 参数跟着候选一起过来（`knobs`）：调这些参数的人在 CNB 那个按钮上，
    # 而真正跑测速的是这台机器。不读它，按钮上的 REFINE_TOP 之类就是摆设。
    knobs = payload.get("knobs") or {}
    if knobs:
        screen_bytes = int(knobs.get("speed_bytes", screen_bytes))
        workers = int(knobs.get("speed_workers", workers))
        timeout = float(knobs.get("speed_timeout", timeout))
        refine_top = int(knobs.get("refine_top", refine_top))
        refine_workers = int(knobs.get("refine_workers", refine_workers))
        refine_bytes = int(knobs.get("refine_bytes", refine_bytes))
        ip_timeout = float(knobs.get("ip_timeout", ip_timeout))
        log(f"参数取自候选文件：初筛 {workers} 并发 × {screen_bytes / 1e6:.1f} MB，"
            f"复测前 {refine_top} 个 × {refine_bytes / 1e6:.0f} MB")

    vantage = _vantage()
    log(f"测速位置：{vantage or '未知'}（GitHub runner 出口）")

    binary = ensure_binary(Path(work_dir) / "bin", log=log)
    mh = Mihomo(binary, Path(work_dir), log=log)

    probes = mh.probe_all(
        candidates, speed_url=speed_url, speed_bytes=screen_bytes,
        speed_timeout=timeout, ip_timeout=ip_timeout, workers=workers, log=log,
    )
    ok = sum(1 for v in probes.values() if v.get("speed", 0) > 0)
    log(f"初筛可用 {ok}/{len(candidates)}")

    refine: dict | None = None
    if refine_top > 0:
        # 复测：更大的下载量 + 更低并发。这里**不传 sources** ——
        # 多源要先直连自检挑源，而本模块不量直连（见模块 docstring）。
        refine = _refine_speed(
            mh, candidates, probes,
            top=refine_top, workers=refine_workers,
            speed_url=speed_url, speed_bytes=refine_bytes,
            speed_timeout=timeout, ip_timeout=ip_timeout,
            screen_bytes=screen_bytes, sources=None, log=log,
        )
        if refine is not None:
            ok = sum(1 for v in probes.values() if v.get("speed", 0) > 0)
            log(f"复测完成，其中 {refine['improved']} 个比初筛更快；总计可用 {ok}")
            _report_refine(refine, log=log)

    out = {
        "run_id": run_id,
        "finished_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %z"),
        "vantage": vantage,
        "host": os.environ.get("RUNNER_NAME") or os.environ.get("HOSTNAME") or "",
        "speed_url": speed_url,
        "screen_bytes": screen_bytes,
        "refine_bytes": refine_bytes,
        "seconds": round(time.time() - started, 1),
        "stats": {
            "total": len(candidates),
            "ok": ok,
            # 复测那一轮的账。CNB 侧读这个字段就能判断「这 80 个速度到底是
            # 24 MB 量出来的还是 6 MB 量出来的」—— 没有它，复测全灭和复测全成
            # 在产物里长得一样。
            "refine": refine,
            "errors": [
                {"count": count, "reason": reason}
                for count, reason in _reason_counts(probes)[:5]
            ],
        },
        "probes": probes,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"结果已写入 {out_path}（{ok}/{len(candidates)} 可用，"
        f"耗时 {out['seconds']:.0f}s）")
    return out
