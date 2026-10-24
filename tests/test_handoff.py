"""两段式流水线的接缝：候选怎么出去、结果怎么回来、错的结果为什么进不来。

这一版把「下载测速」搬到了 GitHub（CNB runner 的出口只有 3~4 MB/s，
节点再快也读成 3~4）。拆成两段之后多了三个只可能静默出错的接缝：

  A. **候选文件够不够下一段用** —— 少了 `server_countries` 或 `stats`，
     finish 段不会报错，只会少判一批国家、报告里的数字变成 0。
  B. **回传结果是不是本轮的** —— `speed.json` 是提交进仓库的文件，上一轮那份
     会一直在。只判「文件存在」的话，GitHub 没跑起来时会安静地读到上一轮的速度，
     产物看着完全正常。必须比对 `run_id`。
  C. **交接提交的作者不能是 cnb-bot** —— 否则最后一轮 `ci_commit.sh` 的
     「防自触发」守卫会把整轮产物当成产物提交跳过（阶段照样显示成功）。

外加一条硬规则：GitHub 侧**不许做直连测速**。那边只准经节点测，
任何 `port=None` 的读数都会被误当成节点速度的一部分。

⚠️ D 段会真的建仓库、真的起一个本地 HTTP 服务、真的 push（目标都是临时目录里的
   本地裸仓库），不联网。缺 git / bash / curl 时直接跳过。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HANDOFF = ROOT / "scripts" / "handoff_github.sh"
GH_PUSH = ROOT / "scripts" / "gh_push.sh"

CANDIDATES = [
    {"name": "n0001", "type": "ss", "server": "1.1.1.1", "port": 443,
     "cipher": "aes-256-gcm", "password": "x", "delay": 120},
    {"name": "n0002", "type": "ss", "server": "2.2.2.2", "port": 443,
     "cipher": "aes-256-gcm", "password": "y", "delay": 200},
    {"name": "n0003", "type": "ss", "server": "3.3.3.3", "port": 443,
     "cipher": "aes-256-gcm", "password": "z", "delay": 300},
]
RUN_ID = "20261024T210000"


def _payload(run_id: str = RUN_ID) -> dict:
    return {
        "run_id": run_id,
        "generated_at": "2026-10-24 21:00:00 +0800",
        "vantage": "中国·广州",
        "delay_url": "https://www.gstatic.com/generate_204",
        "stats": {"raw": 10, "unique": 8, "duplicates": 2, "tcp_alive": 6,
                  "loadable": 6, "rejected": 0, "delay_ok": 3},
        "sources": [],
        "server_countries": {"n0001": "US", "n0002": "US", "n0003": "JP"},
        "candidates": CANDIDATES,
    }


def _speed(run_id: str = RUN_ID) -> dict:
    return {
        "run_id": run_id,
        "finished_at": "2026-10-24 21:05:00 +0800",
        "vantage": "美国·Boydton",
        "stats": {"total": 3, "ok": 3, "errors": []},
        "probes": {
            "n0001": {"speed": 12.5, "speeds": {}, "exit_ip": None, "speed_err": None},
            "n0002": {"speed": 3.2, "speeds": {}, "exit_ip": None, "speed_err": None},
            "n0003": {"speed": 0.0, "speeds": {}, "exit_ip": None,
                      "speed_err": "URLError: timed out"},
        },
    }


# ------------------------------------------------------------------ A. 候选往返


def test_finish_uses_candidates(problems: list[str]) -> None:
    """delay 段写出的字段，finish 段必须全都用得上。"""
    from clashhunter import builder

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "candidates.json").write_text(
            json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
        (out / "speed.json").write_text(
            json.dumps(_speed(), ensure_ascii=False), encoding="utf-8")
        result = builder.build(
            out_dir=out, work_dir=out / "work", stage="finish",
            candidates_path=out / "candidates.json",
            speed_path=out / "speed.json", log=lambda *a, **k: None,
        )

    names = [p["name"] for p in result.proxies]
    if len(names) != 2:
        problems.append(f"finish 段应只剩 2 个有速度的节点，实际 {names}")
    if not any(n.startswith("美国-01-12.5") for n in names):
        problems.append(f"节点名没有按速度重命名：{names}")
    if result.stats.delay_ok != 3 or result.stats.raw != 10:
        problems.append(f"上游计数没有从 candidates.json 带过来：{result.stats}")
    if result.stats.speed_ok != 2:
        problems.append(f"speed_ok 应为 2，实际 {result.stats.speed_ok}")
    # 两段在不同机器上跑，头部必须同时写两个位置 —— 只写延迟那台会让人
    # 把「GitHub runner → 节点」的速度当成「国内 → 节点」的速度
    if "中国·广州" not in result.vantage or "美国·Boydton" not in result.vantage:
        problems.append(f"vantage 没有同时标出两段的位置：{result.vantage!r}")


def test_finish_rejects_stale_run_id(problems: list[str]) -> None:
    """B. 上一轮的 speed.json 必须被拒绝，不能拿来出产物。"""
    from clashhunter import builder

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "candidates.json").write_text(
            json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
        (out / "speed.json").write_text(
            json.dumps(_speed("20261023T000000"), ensure_ascii=False), encoding="utf-8")
        try:
            builder.build(out_dir=out, work_dir=out / "work", stage="finish",
                          candidates_path=out / "candidates.json",
                          speed_path=out / "speed.json", log=lambda *a, **k: None)
        except SystemExit as exc:
            if "run_id" not in str(exc):
                problems.append(f"拒绝的理由没说清是 run_id 不一致：{exc}")
        else:
            problems.append("run_id 不一致时居然成功出产物了 —— "
                            "GitHub 没跑起来的那天会安静地沿用上一轮的速度")


# ------------------------------------------------------- C. 不许直连测速（GitHub）


def test_speedjob_never_measures_locally(problems: list[str]) -> None:
    """GitHub 侧只准经节点测：直连读数会被当成节点速度的一部分。

    用 AST 而不是文本搜索：这个文件的 docstring 里**专门写了**「不许调用
    measure_aggregate / measure_sources / _pick_sources」，文本搜索会把
    那段说明本身当成违规。判据必须是「代码里有没有真的调用」。
    """
    import ast

    src = (ROOT / "clashhunter" / "speedjob.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {
        "measure_aggregate": "直连并发总量 = 测 GitHub runner 自己的管道",
        "measure_sources": "直连测源 = 测 runner 自己的出口",
        "_pick_sources": "挑源要先直连自检",
    }
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            hits.append(f"{node.attr}（{banned[node.attr]}）")
        if isinstance(node, ast.Name) and node.id in banned:
            hits.append(f"{node.id}（{banned[node.id]}）")
        # measure_speed(None, ...) / measure_speed_ex(None, ...) 就是直连
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("measure_speed", "measure_speed_ex", "probe_one") \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value is None:
            hits.append(f"{node.func.attr}(None, ...) —— port=None 就是直连")
    for hit in hits:
        problems.append(f"speedjob.py 里调用了 {hit} —— 这里只准经节点测速")
    if "SPEED_URL" not in src:
        problems.append("speedjob.py 没有用 SPEED_URL（Cloudflare）")


def test_speedjob_writes_run_id(problems: list[str]) -> None:
    """回传结果必须带 run_id，否则 CNB 没法判断是不是本轮的。"""
    text = (ROOT / "clashhunter" / "speedjob.py").read_text(encoding="utf-8")
    if '"run_id": run_id' not in text:
        problems.append("speed.json 里没有 run_id 字段")
    if "if not run_id:" not in text:
        problems.append("speedjob.run() 没有校验输入里的 run_id")


# ------------------------------------------------------------ D. 交接脚本端到端


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@local",
         "-c", "init.defaultBranch=main", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401,ANN001
        return


def _serve(directory: Path, port: int) -> ThreadingHTTPServer:
    handler = lambda *a, **k: _Quiet(*a, directory=str(directory), **k)  # noqa: E731
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _seed_handoff(tmp: Path, served: dict) -> tuple[Path, Path, Path]:
    """建裸仓库 + 检出 + 一个提供 speed.json 的本地 HTTP 服务。"""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                   capture_output=True)
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    run = tmp / "run"
    _git(tmp, "clone", "-q", str(origin), str(run))
    (run / "dist").mkdir(parents=True, exist_ok=True)
    (run / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(HANDOFF, run / "scripts" / "handoff_github.sh")
    (run / "dist" / "candidates.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    _git(run, "add", "-A")
    _git(run, "commit", "-qm", "init")
    _git(run, "branch", "-M", "main")
    _git(run, "push", "-q", "origin", "main")

    web = tmp / "web"
    (web / "dist").mkdir(parents=True, exist_ok=True)
    (web / "dist" / "speed.json").write_text(
        json.dumps(served, ensure_ascii=False), encoding="utf-8")
    return origin, run, web


def test_handoff_roundtrip(problems: list[str]) -> None:
    """D1. 正确的 run_id ⇒ 结果落地、裸仓库的 ci-handoff 收到候选。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin, run, web = _seed_handoff(tmp, _speed())
        port = _free_port()
        httpd = _serve(web, port)
        try:
            got = subprocess.run(
                ["bash", str(run / "scripts" / "handoff_github.sh")],
                cwd=str(run), capture_output=True, text=True, timeout=120,
                env={**__import__("os").environ,
                     "GITHUB_TOKEN": "fake-token",
                     "GITHUB_URL": str(origin),
                     "HANDOFF_RAW_BASE": f"http://127.0.0.1:{port}",
                     "HANDOFF_INTERVAL": "1",
                     "HANDOFF_TIMEOUT_S": "20",
                     "SPEED_OUT": "dist/speed.json"},
            )
        finally:
            httpd.shutdown()

        if got.returncode != 0:
            problems.append(f"交接失败（退出码 {got.returncode}）："
                            f"{got.stdout[-400:]}{got.stderr[-300:]}")
            return
        speed_file = run / "dist" / "speed.json"
        if not speed_file.is_file():
            problems.append("交接成功但 dist/speed.json 没落地")
        else:
            data = json.loads(speed_file.read_text(encoding="utf-8"))
            if data.get("run_id") != RUN_ID:
                problems.append(f"落地的是别的轮次：{data.get('run_id')}")

        # 候选必须真的推到了 ci-handoff，否则 GitHub 那边根本不会启动
        branches = _git(origin, "branch", "--list", check=False).stdout
        if "ci-handoff" not in branches:
            problems.append(f"裸仓库里没有 ci-handoff 分支：{branches!r}")
            return
        shown = _git(origin, "show", "ci-handoff:dist/candidates.json").stdout
        if RUN_ID not in shown:
            problems.append("ci-handoff 上的 candidates.json 不是本轮的")

        # C. 交接提交的作者**不能**是 cnb-bot，否则最后一轮 ci_commit 会被
        #    「防自触发」守卫整个跳过（阶段照样成功，产物却没提交）
        author = _git(run, "log", "-1", "--format=%ae").stdout.strip()
        if author == "cnb-bot@cnb.cool":
            problems.append("交接提交用了 cnb-bot 邮箱 —— 最后的 ci_commit 会静默跳过")


def test_handoff_rejects_other_round(problems: list[str]) -> None:
    """D2. 只有上一轮的 speed.json ⇒ 必须等到超时后失败，不能拿旧的顶上。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin, run, web = _seed_handoff(tmp, _speed("20261023T000000"))
        port = _free_port()
        httpd = _serve(web, port)
        try:
            got = subprocess.run(
                ["bash", str(run / "scripts" / "handoff_github.sh")],
                cwd=str(run), capture_output=True, text=True, timeout=120,
                env={**__import__("os").environ,
                     "GITHUB_TOKEN": "fake-token",
                     "GITHUB_URL": str(origin),
                     "HANDOFF_RAW_BASE": f"http://127.0.0.1:{port}",
                     "HANDOFF_INTERVAL": "1",
                     "HANDOFF_TIMEOUT_S": "3",
                     "SPEED_OUT": "dist/speed.json"},
            )
        finally:
            httpd.shutdown()
        if got.returncode == 0:
            problems.append("拿到的是上一轮的结果却当成功退出了")
        if (run / "dist" / "speed.json").exists():
            problems.append("上一轮的结果被写成了本轮的 dist/speed.json")


def test_handoff_requires_token(problems: list[str]) -> None:
    """D3. 没有 GITHUB_TOKEN ⇒ 明确失败并说清要配什么，不要推一半。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin, run, _web = _seed_handoff(tmp, _speed())
        got = subprocess.run(
            ["bash", str(run / "scripts" / "handoff_github.sh")],
            cwd=str(run), capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ, "GITHUB_TOKEN": "",
                 "GITHUB_URL": str(origin)},
        )
        if got.returncode == 0:
            problems.append("没有 GITHUB_TOKEN 也退出了 0")
        if "GITHUB_TOKEN" not in got.stderr:
            problems.append("失败信息里没有提到 GITHUB_TOKEN")


def test_scripts_do_not_leak_token(problems: list[str]) -> None:
    """令牌不能出现在 URL 里 —— git 出错时会把整个 URL 打进日志。"""
    for path in (HANDOFF, GH_PUSH):
        text = path.read_text(encoding="utf-8")
        if "x-access-token:${TOKEN}@" in text or "${TOKEN}@github.com" in text:
            problems.append(f"{path.name} 把令牌拼进了 URL")
        if "http.extraheader" not in text:
            problems.append(f"{path.name} 没有用 http.extraheader 传凭据")


# ---------------------------------------------------------------- E. 流水线顺序


def test_cnb_stage_order(problems: list[str]) -> None:
    """CNB 阶段顺序：延迟 → 交接 → 收尾 → 提交 CNB → 复制到 GitHub。"""
    import yaml

    cfg = yaml.safe_load((ROOT / ".cnb.yml").read_text(encoding="utf-8"))
    stages = cfg["main"]["web_trigger_daily"][0]["stages"]
    scripts = [s.get("script", "") for s in stages]
    joined = "\n".join(scripts)

    order = [
        ("--stage delay", "延迟测速段"),
        ("handoff_github.sh", "交给 GitHub 测速"),
        ("--stage finish", "收尾生成产物"),
        ("ci_commit.sh", "提交回 CNB"),
        ("gh_push.sh", "复制到 GitHub"),
    ]
    positions = []
    for needle, label in order:
        idx = joined.find(needle)
        if idx < 0:
            problems.append(f".cnb.yml 里找不到「{label}」（{needle}）")
            return
        positions.append((idx, label))
    if positions != sorted(positions):
        problems.append("阶段顺序不对："
                        + " → ".join(label for _, label in sorted(positions)))
    if "main.py \\" in joined and "--stage delay" not in scripts[0] and \
            "--stage delay" not in joined.split("main.py")[0]:
        pass  # 顺序已由上一条覆盖


def main() -> int:
    problems: list[str] = []
    test_finish_uses_candidates(problems)
    test_finish_rejects_stale_run_id(problems)
    test_speedjob_never_measures_locally(problems)
    test_speedjob_writes_run_id(problems)
    if _have("git") and _have("bash") and _have("curl"):
        test_handoff_roundtrip(problems)
        test_handoff_rejects_other_round(problems)
        test_handoff_requires_token(problems)
    else:
        print("· 跳过交接脚本测试：本机缺 git / bash / curl")
    test_scripts_do_not_leak_token(problems)
    test_cnb_stage_order(problems)

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 两段式接缝通过：候选字段够收尾用、上一轮的结果进不来、"
          "GitHub 侧不做直连测速、交接提交不冒充 cnb-bot、令牌不进 URL、"
          "CNB 阶段顺序正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
