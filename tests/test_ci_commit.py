"""`scripts/ci_commit.sh` 的推送路径 —— 远端在构建期间前进时，产物还能不能落地。

为什么值得单独钉住：这个脚本是**整轮流水线的最后一步**，它的失败方式是静默的 ——
阶段变红、产物文件停在上一轮的时间戳，从外面看和「构建根本没跑」长得一模一样。
2026-10-24 本地手工推的时候连着踩了三次 `! [rejected] (fetch first)`：一轮构建
十几分钟，这期间远端必然可能动（人推代码 / 另一个流水线刚提交了产物），
而旧版脚本是「commit 完直接 push，被拒就换带凭据的地址再 push 一次」——
两次都打在同一个旧基线上，必然同样被拒。

四条断言，全部用本地裸仓库模拟，不联网：

  A. 远端前进一格但没碰 `dist/` ⇒ fetch + rebase 后重试，产物要真的落地；
  B. 远端也改了 `dist/`（两个流水线同时写）⇒ 退出码非 0、不留 rebase 残局、
     远端内容不被破坏 —— 宁可这轮产物丢掉，也不能把别人的产物覆盖掉；
  C. 防自触发守卫还在：上一提交是 cnb-bot 且只改 `dist/` ⇒ 跳过、不产生新提交；
  D. 源码里 push 之前确实有 fetch + rebase（有人删掉重试会在这里被抓住）。

⚠️ 这个测试会真的建仓库、真的 push，但全部落在临时目录里的**本地裸仓库**上。
   缺 git 或 bash 时直接跳过（打印一行），不让它变成环境相关的红灯。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
SCRIPT = ROOT / "scripts" / "ci_commit.sh"

from _bash import find_bash  # noqa: E402  （要先补 sys.path）

BASH = find_bash()


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在 `repo` 里跑 git。身份用 -c 传，避免依赖（也不污染）全局配置。"""
    return subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@local",
         "-c", "init.defaultBranch=main", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _seed(tmp: Path) -> tuple[Path, Path]:
    """建一个裸仓库 + 一份「runner 检出」，返回 (裸仓库, 检出)。"""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                   capture_output=True)
    # 空裸仓库的 HEAD 默认指 master，克隆出来是空检出 —— 显式指到 main
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    run = tmp / "run"
    _git(tmp, "clone", "-q", str(origin), str(run))
    (run / "dist").mkdir(parents=True, exist_ok=True)
    (run / "scripts").mkdir(parents=True, exist_ok=True)
    (run / "dist" / "probe.json").write_text("v1\n", encoding="utf-8")
    shutil.copy(SCRIPT, run / "scripts" / "ci_commit.sh")
    _git(run, "add", "-A")
    _git(run, "commit", "-qm", "init")
    _git(run, "branch", "-M", "main")
    _git(run, "push", "-q", "origin", "main")
    return origin, run


def _commit_script(run: Path, msg: str) -> subprocess.CompletedProcess:
    # 路径写成 POSIX 形式：交给 bash 的 `D:\...` 会把反斜杠吃掉（见 _bash.py）
    script = (run / "scripts" / "ci_commit.sh").as_posix()
    return subprocess.run([BASH, script, msg],
                          cwd=str(run), capture_output=True, text=True)


def _remote_show(origin: Path, path: str) -> str:
    return _git(origin, "show", f"main:{path}").stdout.strip()


def _remote_top(origin: Path) -> str:
    return _git(origin, "log", "-1", "--format=%s", "main").stdout.strip()


def test_remote_moved_code(problems: list[str]) -> None:
    """A. 远端前进但没碰 dist/ ⇒ rebase 重试后产物必须落地。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin, run = _seed(tmp)
        # 另一个人（或另一个流水线）在构建期间推了代码
        other = tmp / "human"
        _git(tmp, "clone", "-q", str(origin), str(other))
        (other / "fix.txt").write_text("hotfix\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "hotfix during build")
        _git(other, "push", "-q", "origin", "HEAD:main")

        (run / "dist" / "probe.json").write_text("v2\n", encoding="utf-8")
        got = _commit_script(run, "chore(probe): case-a")
        if got.returncode != 0:
            problems.append(
                "远端前进一格后产物提交失败（退出码 "
                f"{got.returncode}）—— 旧版脚本两次 push 都打在同一个旧基线上，"
                f"这轮产物就丢了。输出：{got.stdout[-300:]}{got.stderr[-200:]}")
        if _remote_show(origin, "dist/probe.json") != "v2":
            problems.append("远端 dist/probe.json 不是本轮产物 —— rebase 后没有真的推上去")
        if _remote_top(origin) != "chore(probe): case-a":
            problems.append(f"远端最新提交是 {_remote_top(origin)!r}，"
                            f"不是产物提交 —— 产物没有落在别人代码之上")


def test_remote_touched_dist(problems: list[str]) -> None:
    """B. 远端也改了 dist/ ⇒ 真冲突，必须失败退出且不留残局。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin, run = _seed(tmp)
        other = tmp / "other"
        _git(tmp, "clone", "-q", str(origin), str(other))
        (other / "dist" / "probe.json").write_text("别人的产物\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "chore(probe): other pipeline")
        _git(other, "push", "-q", "origin", "HEAD:main")

        # 先落一个非 dist 的提交，免得被「防自触发」守卫提前挡掉（守卫只看上一提交）
        (run / "code.txt").write_text("code\n", encoding="utf-8")
        _git(run, "add", "code.txt")
        _git(run, "commit", "-qm", "feat: code")
        (run / "dist" / "probe.json").write_text("v3\n", encoding="utf-8")

        got = _commit_script(run, "chore(probe): case-b")
        if got.returncode == 0:
            problems.append("远端也改了 dist/ 时应当失败退出 —— "
                            "成功退出会让流水线显示绿色，而产物其实不是本轮的")
        if (run / ".git" / "rebase-merge").exists() or (run / ".git" / "rebase-apply").exists():
            problems.append("冲突后残留 rebase 状态 —— 下一次 commit 会带着冲突标记走")
        if _remote_show(origin, "dist/probe.json") != "别人的产物":
            problems.append("冲突时把别人的产物覆盖掉了 —— 宁可丢本轮产物也不能覆盖")


def test_self_trigger_guard(problems: list[str]) -> None:
    """C. 上一提交是 cnb-bot 且只改 dist/ ⇒ 跳过，不产生新提交（防自触发）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin, run = _seed(tmp)
        (run / "dist" / "probe.json").write_text("v2\n", encoding="utf-8")
        first = _commit_script(run, "chore(probe): first")
        if first.returncode != 0:
            problems.append(f"第一次产物提交就失败了：{first.stdout[-300:]}")
            return
        before = _remote_top(origin)
        count_before = _git(origin, "rev-list", "--count", "main").stdout.strip()

        # 模拟「产物提交又触发了一次构建」：同样的检出、同样只改 dist/
        (run / "dist" / "probe.json").write_text("v3\n", encoding="utf-8")
        second = _commit_script(run, "chore(probe): second")
        if second.returncode != 0:
            problems.append(f"守卫分支应当正常退出（0），实际 {second.returncode}")
        if _remote_top(origin) != before:
            problems.append("防自触发守卫失效 —— 产物提交又推了一次，会无限循环")
        if _git(origin, "rev-list", "--count", "main").stdout.strip() != count_before:
            problems.append("远端提交数变了 —— 守卫没有真的跳过")


def test_push_retries_after_rebase(problems: list[str]) -> None:
    """D. 源码断言：push 之前必须有 fetch + rebase，且带重试。"""
    text = SCRIPT.read_text(encoding="utf-8")
    if "rebase FETCH_HEAD" not in text or "git fetch" not in text:
        problems.append("ci_commit.sh 的推送路径里没有 fetch + rebase —— "
                        "远端前进一格就会让整轮产物丢掉")
    if "sync_and_push" not in text:
        problems.append("找不到 sync_and_push —— 默认地址和带凭据地址应当共用同一套重试")
    if text.count("sync_and_push ") < 2:
        problems.append("带凭据的重试没有走同一套 fetch + rebase")


def main() -> int:
    if not (_have("git") and _have("bash")):
        print("· 跳过 ci_commit 推送测试：本机没有 git 或 bash")
        return 0
    if not SCRIPT.exists():
        print("✗ 找不到 scripts/ci_commit.sh")
        return 1

    problems: list[str] = []
    test_remote_moved_code(problems)
    test_remote_touched_dist(problems)
    test_self_trigger_guard(problems)
    test_push_retries_after_rebase(problems)
    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ ci_commit 推送路径通过：远端前进后 rebase 重试能落地、真冲突时失败退出"
          "且不覆盖别人的产物、防自触发守卫仍在、两条推送路径共用重试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
