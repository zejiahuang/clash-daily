"""两段式流水线的接缝：候选怎么出去、结果怎么回来、错的结果为什么进不来。

这一版把「下载测速」搬到了 GitHub（CNB runner 的出口只有 3~4 MB/s，
节点再快也读成 3~4）。拆成两段之后多了几个只可能静默出错的接缝：

  A. **候选文件够不够下一段用** —— 少了 `knobs` 或 `server_countries`，
     finish 段不会报错，只会少判一批国家、测速参数变成默认值。
  B. **回传结果是不是本轮的** —— `speed.json` 是提交进仓库的文件，上一轮那份
     会一直在。只判「文件存在」的话，GitHub 没跑起来时会安静地读到上一轮的速度，
     产物看着完全正常。必须比对 `run_id`。
  C. **整条链路不能有任何密钥** —— 这个仓库是公开的：写一个 GitHub PAT 进去
     等于把写权限发给所有人，而且 GitHub 的密钥扫描会把公开仓库里出现的
     `ghp_` 令牌**自动吊销**。所以方向是「GitHub 自己来取」，CNB 只读公开 raw。
  D. **「发布候选」那一步必须能穿过防自触发守卫** —— 它紧跟在上一轮的产物提交
     之后，按守卫的判据会被当成产物提交跳过，于是候选永远发不出去、整轮卡死。
     同时它的作者**不能**是 cnb-bot，否则最后那次真正的产物提交会被守卫拦住。

⚠️ 等待脚本那两条会真的起一个本地 HTTP 服务，守卫那条会真的建仓库并 push
   （目标都是临时目录里的本地裸仓库），不联网。缺 git / bash / curl 时直接跳过。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 同目录的 _bash.py：用 `python tests/test_xxx.py` 跑时它本来就在 sys.path[0]，
# 但 pytest 之类会换掉 rootdir，所以显式加一次。
sys.path.insert(0, str(ROOT / "tests"))

WAIT = ROOT / "scripts" / "wait_speed.sh"
CI_COMMIT = ROOT / "scripts" / "ci_commit.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "speedtest.yml"

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
        "vantage": "中国·广东·广州",
        "delay_url": "https://www.gstatic.com/generate_204",
        "stats": {"raw": 10, "unique": 8, "duplicates": 2, "tcp_alive": 6,
                  "loadable": 6, "rejected": 0, "delay_ok": 3},
        "sources": [],
        "server_countries": {"n0001": "US", "n0002": "US", "n0003": "JP"},
        "knobs": {"speed_bytes": 6_000_000, "speed_workers": 32},
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


class _FakeMihomo:
    """只实现 delay 段会用到的那四个方法。"""

    def __init__(self, *a, **k):
        pass

    def filter_loadable(self, proxies):
        return proxies, []

    def start(self, proxies, ports=None, **k):
        return None

    def stop(self):
        return None

    def delay(self, name, url=None, timeout_ms=None):
        return 100 + int(name[1:])  # n0001 → 101


def test_delay_stage_writes_usable_file(problems: list[str]) -> None:
    """A0. 真跑一遍 delay 段（源站/内核全打桩），产出的文件必须能被 finish 吃下。

    为什么不能只测 finish：`_finish` 的输入是我手写的假 payload，
    真生产者少写一个字段它照样能过 —— 而流水线里那一跑要十几分钟才暴露。
    """
    import types
    from unittest.mock import patch

    from clashhunter import builder

    proxies = [dict(item) for item in CANDIDATES]
    report = types.SimpleNamespace(sid="fake", urls=["u"], articles=["a"],
                                   proxies=proxies, error=None, note="")
    alive = {(p["server"], p["port"]): True for p in proxies}

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        with patch("clashhunter.builder.source_mod.collect", return_value=[report]), \
                patch("clashhunter.builder.netcheck.probe_endpoints", return_value=alive), \
                patch("clashhunter.builder.ensure_binary", return_value=Path("/bin/true")), \
                patch("clashhunter.builder.Mihomo", _FakeMihomo), \
                patch("clashhunter.builder._server_countries",
                      return_value={"n0001": "US", "n0002": "US", "n0003": "JP"}):
            result = builder.build(
                out_dir=out, work_dir=out / "work", stage="delay",
                speed_bytes=6_000_000, speed_workers=8, refine_top=24,
                candidates_path=out / "candidates.json", log=lambda *a, **k: None,
            )
        if result.proxies:
            problems.append("delay 段不该产出节点（那是 finish 段的活）")
        path = out / "candidates.json"
        if not path.is_file():
            problems.append("delay 段没有写出 candidates.json")
            return
        payload = json.loads(path.read_text(encoding="utf-8"))

    for key in ("run_id", "generated_at", "candidates", "server_countries", "stats", "knobs"):
        if key not in payload:
            problems.append(f"candidates.json 缺 {key} —— finish 段会静默少东西")
    if len(payload.get("candidates") or []) != 3:
        problems.append(f"候选数不对：{len(payload.get('candidates') or [])}")
    if any("delay" not in c for c in payload.get("candidates") or []):
        problems.append("候选里没有 delay 字段 —— 报告里的延迟列会是空的")
    knobs = payload.get("knobs") or {}
    if knobs.get("refine_top") != 24 or knobs.get("speed_workers") != 8:
        problems.append(f"knobs 没把测速参数带过去：{knobs}")


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
        builder.write_outputs(result, out, log=lambda *a, **k: None)
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))

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
    if "中国·广东·广州" not in result.vantage or "美国·Boydton" not in result.vantage:
        problems.append(f"vantage 没有同时标出两段的位置：{result.vantage!r}")
    # run_id 要一路写进 report.json：GitHub 那边靠它判断「CNB 收尾跑完没有」
    if report.get("run_id") != RUN_ID:
        problems.append(f"report.json 里的 run_id 不对：{report.get('run_id')!r}")


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


# ------------------------------------------------------------ C2. 全程不许有密钥


# 只看「代码里有没有真的用」，不看「文里有没有出现这个词」。
# ⚠️ 这条界线必须画清楚：`wait_speed.sh` 的头部、`.cnb.yml` 的头部、
#    `speedtest.yml` 的头部**都专门用整段话解释「为什么这里不放令牌」**，
#    一旦写成 `"GITHUB_TOKEN" in text`，那三段说明本身就成了违规 ——
#    和 `test_speedjob_never_measures_locally` 当初撞上 docstring 是同一个坑
#    （那次已经改成 AST，这次改成「剥注释 + 只认可执行写法」）。
_CRED_RULES: tuple[tuple[str, str], ...] = (
    (r"\bGITHUB_TOKEN\s*=", "给 GITHUB_TOKEN 赋值"),
    (r"\bGH_TOKEN\s*=", "给 GH_TOKEN 赋值"),
    (r"\bsecrets\s*\.", "引用 secrets."),
    (r"x-access-token", "用 x-access-token 拼凭据 URL"),
    (r"ghp_[A-Za-z0-9]{8,}", "硬编码了 GitHub 令牌"),
    # 只认**字面**凭据：`https://cnb:${CNB_TOKEN}@…` 这种从环境变量拼出来的不算
    # —— 变量由 CNB 自己注入，仓库里没有可泄露的东西。
    (r"https?://[^/\s:@$]+:[^/\s@$]+@", "URL 里内嵌了字面账号密码"),
)


def _strip_shell_comments(text: str) -> str:
    """剥掉 shell 注释，只留会被执行的那部分文本。"""
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        # 行尾注释：`#` 前面是空白才算（URL 里的 `#` 不属于这种情况）
        out.append(re.sub(r"(?<=\s)#.*$", "", line))
    return "\n".join(out)


def _scan_credentials(text: str) -> list[str]:
    return [label for pat, label in _CRED_RULES if re.search(pat, text)]


def test_no_credentials_anywhere(problems: list[str]) -> None:
    """C2. 整条链路不许**真的用**令牌：仓库公开，写进去等于公开，且会被自动吊销。"""
    import yaml

    # 对照样本 —— 没有它，「零命中」可能只是正则写错了。
    # 这个项目已经两次因为「探针本身失效」而误判（见 README「通用教训」）。
    control = _scan_credentials("export GITHUB_TOKEN=ghp_" + "a" * 20)
    if len(control) < 2:
        problems.append(f"凭据扫描器自己失效了（对照样本只命中 {control}）—— "
                        f"这样「零命中」不能证明任何事")
        return

    for path in (WAIT, CI_COMMIT):
        text = _strip_shell_comments(path.read_text(encoding="utf-8"))
        for label in _scan_credentials(text):
            problems.append(f"{path.name} 里{label} —— 仓库是公开的，写进去等于"
                            f"把写权限发出去，而且会被密钥扫描自动吊销")

    # YAML 解析出来的结构里**没有注释**，剩下的每一处都是真会跑的东西
    for path in (WORKFLOW, ROOT / ".cnb.yml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for label in _scan_credentials(json.dumps(data, ensure_ascii=False)):
            problems.append(f"{path.name} 里{label} —— 这条链路应该零密钥")


# ---------------------------------------------------------------- D. 等待与触发


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _bash() -> str:
    """挑一个**原生** bash（本机 PATH 里的 `bash` 可能是 WSL 那个，见 _bash.py）。"""
    from _bash import find_bash

    return find_bash()


BASH = _bash()


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


def _run_wait(tmp: Path, served: dict, *, timeout_s: int = 8) -> subprocess.CompletedProcess:
    web = tmp / "web"
    (web / "dist").mkdir(parents=True, exist_ok=True)
    (web / "dist" / "speed.json").write_text(
        json.dumps(served, ensure_ascii=False), encoding="utf-8")
    port = _free_port()
    httpd = _serve(web, port)
    try:
        return subprocess.run(
            [BASH, WAIT.as_posix(), "dist/candidates.json"],
            cwd=str(tmp), capture_output=True, text=True, timeout=120,
            env={**os.environ,
                 "WAIT_RAW_BASE": f"http://127.0.0.1:{port}",
                 "WAIT_INTERVAL": "1",
                 "WAIT_TIMEOUT_S": str(timeout_s),
                 "SPEED_OUT": "dist/speed.json"},
        )
    finally:
        httpd.shutdown()


def _seed_workdir(tmp: Path) -> None:
    (tmp / "dist").mkdir(parents=True, exist_ok=True)
    (tmp / "dist" / "candidates.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")


def test_wait_speed_accepts_matching_round(problems: list[str]) -> None:
    """D1. run_id 对上 ⇒ 结果落地；全程不需要任何令牌。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_workdir(tmp)
        got = _run_wait(tmp, _speed())
        if got.returncode != 0:
            problems.append(f"等待脚本失败（退出码 {got.returncode}）："
                            f"{got.stdout[-400:]}{got.stderr[-300:]}")
            return
        speed_file = tmp / "dist" / "speed.json"
        if not speed_file.is_file():
            problems.append("等待成功但 dist/speed.json 没落地")
        else:
            data = json.loads(speed_file.read_text(encoding="utf-8"))
            if data.get("run_id") != RUN_ID:
                problems.append(f"落地的是别的轮次：{data.get('run_id')}")
        if "GITHUB_TOKEN" in got.stdout or "GITHUB_TOKEN" in got.stderr:
            problems.append("等待脚本还在要令牌 —— 这条链路应该零密钥")


def test_wait_speed_rejects_other_round(problems: list[str]) -> None:
    """D2. 只有上一轮的 speed.json ⇒ 必须等到超时后失败，不能拿旧的顶上。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_workdir(tmp)
        got = _run_wait(tmp, _speed("20261023T000000"), timeout_s=3)
        if got.returncode == 0:
            problems.append("拿到的是上一轮的结果却当成功退出了")
        if (tmp / "dist" / "speed.json").exists():
            problems.append("上一轮的结果被写成了本轮的 dist/speed.json")


def test_workflow_shape(problems: list[str]) -> None:
    """D3. 工作流的形状：cron 轮询 + 从 CNB 取代码 + 用 run_id 挡掉空跑。"""
    import yaml

    cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = cfg.get(True) or cfg.get("on") or {}
    if "schedule" not in on:
        problems.append("speedtest.yml 不是 cron 轮询触发的 —— CNB 侧没有令牌，推不动它")
    scripts = "\n".join(s.get("run", "") for s in cfg["jobs"]["speed"]["steps"])
    if "cnb.cool" not in scripts or "git clone" not in scripts:
        problems.append("工作流没有从 CNB 取代码 —— 它会跑本仓库里那份可能落后的副本")
    if "run_id" not in scripts:
        problems.append("工作流没有用 run_id 判断「有没有新的一轮」，cron 每次都会白测一遍")
    if "report.json" not in scripts:
        problems.append("工作流没有等 CNB 收尾（report.json），复制产物会拿到上一轮的")


# ---------------------------------------------------------------- E. 流水线顺序


def test_cnb_stage_order(problems: list[str]) -> None:
    """CNB 阶段顺序：延迟 → 发布候选 → 等 GitHub → 收尾 → 校验 → 提交。"""
    import yaml

    cfg = yaml.safe_load((ROOT / ".cnb.yml").read_text(encoding="utf-8"))
    stages = cfg["main"]["web_trigger_daily"][0]["stages"]
    joined = "\n".join(s.get("script", "") for s in stages)

    order = [
        ("--stage delay", "延迟测速段"),
        ("CI_FORCE=1", "发布候选（要穿过防自触发守卫）"),
        ("wait_speed.sh", "等 GitHub 测速"),
        ("--stage finish", "收尾生成产物"),
        ("verify_dist.py", "校验产物"),
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
    # 提交回仓库必须是最后一步，而且在校验之后
    if joined.count("ci_commit.sh") != 2:
        problems.append(f"ci_commit.sh 应被调用两次（发布候选 + 产物），实际 "
                        f"{joined.count('ci_commit.sh')} 次")
    elif joined.rfind("ci_commit.sh") < joined.find("verify_dist.py"):
        problems.append("产物提交排在校验之前 —— 校验失败时脏产物已经推上去了")


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@local",
         "-c", "init.defaultBranch=main", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def test_ci_commit_force(problems: list[str]) -> None:
    """D4. 守卫会挡住「发布候选」，CI_FORCE 必须能穿过去，且作者不能是 cnb-bot。

    这是整轮最容易卡死的地方：发布候选紧跟在上一轮的产物提交之后，
    守卫的判据（上一提交是 cnb-bot 且只改 dist/）正好命中 ⇒ 候选发不出去
    ⇒ GitHub 看不到新一轮 ⇒ CNB 一直等 ⇒ 整轮超时。**全程没有任何报错。**
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin = tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                       capture_output=True)
        _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        run = tmp / "run"
        _git(tmp, "clone", "-q", str(origin), str(run))
        (run / "dist").mkdir(parents=True, exist_ok=True)
        (run / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copy(CI_COMMIT, run / "scripts" / "ci_commit.sh")
        (run / "dist" / "probe.json").write_text("v1\n", encoding="utf-8")
        _git(run, "add", "-A")
        _git(run, "commit", "-qm", "init")
        _git(run, "branch", "-M", "main")
        _git(run, "push", "-q", "origin", "main")

        # 造一个「上一提交是 cnb-bot 且只改 dist/」的局面（上一轮产物提交的样子）
        (run / "dist" / "probe.json").write_text("v2\n", encoding="utf-8")
        _git(run, "add", "-A")
        _git(run, "-c", "user.name=cnb-bot", "-c", "user.email=cnb-bot@cnb.cool",
             "commit", "-qm", "chore(probe): 上一轮的产物")
        before = _git(run, "rev-list", "--count", "main").stdout.strip()

        # 不带 CI_FORCE ⇒ 被守卫挡住
        got = subprocess.run([BASH, "scripts/ci_commit.sh", "chore(handoff): 候选"],
                             cwd=str(run), capture_output=True, text=True)
        if got.returncode != 0:
            problems.append(f"被守卫挡住时应当正常退出（0），实际 {got.returncode}")
        if _git(run, "rev-list", "--count", "main").stdout.strip() != before:
            problems.append("没有 CI_FORCE 时守卫没生效 —— 产物提交会被重复推送")

        # 带 CI_FORCE + ci@local ⇒ 必须推上去，而且作者不是 cnb-bot
        (run / "dist" / "candidates.json").write_text('{"run_id":"x"}\n', encoding="utf-8")
        got = subprocess.run(
            [BASH, "scripts/ci_commit.sh", "chore(handoff): 候选"],
            cwd=str(run), capture_output=True, text=True,
            env={**os.environ, "CI_FORCE": "1",
                 "CI_AUTHOR_NAME": "clash-daily-ci", "CI_AUTHOR_EMAIL": "ci@local"},
        )
        if got.returncode != 0:
            problems.append(f"CI_FORCE=1 时仍失败：{got.stdout[-300:]}{got.stderr[-200:]}")
            return
        if _git(origin, "show", "main:dist/candidates.json", check=False).returncode != 0:
            problems.append("CI_FORCE=1 时候选没有真的推上去")
        author = _git(origin, "log", "-1", "--format=%ae", "main").stdout.strip()
        if author == "cnb-bot@cnb.cool":
            problems.append("发布候选用了 cnb-bot 邮箱 —— 最后那次产物提交会被守卫拦住")


def main() -> int:
    problems: list[str] = []
    test_delay_stage_writes_usable_file(problems)
    test_finish_uses_candidates(problems)
    test_finish_rejects_stale_run_id(problems)
    test_speedjob_never_measures_locally(problems)
    test_speedjob_writes_run_id(problems)
    test_no_credentials_anywhere(problems)
    if _have("git") and _have("bash") and _have("curl"):
        # 打印用的是哪个 bash：本机 PATH 里的 `bash` 可能是 WSL，行为不一样
        print(f"· 脚本测试用的 bash：{BASH}")
        test_wait_speed_accepts_matching_round(problems)
        test_wait_speed_rejects_other_round(problems)
        test_ci_commit_force(problems)
    else:
        print("· 跳过脚本测试：本机缺 git / bash / curl")
    test_workflow_shape(problems)
    test_cnb_stage_order(problems)

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 两段式接缝通过：候选字段够收尾用、上一轮的结果进不来、"
          "GitHub 侧不做直连测速、全程零密钥、发布候选能穿过守卫且不冒充 cnb-bot、"
          "工作流按 run_id 挡空跑、CNB 阶段顺序正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
