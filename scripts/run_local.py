#!/usr/bin/env python3
"""跨平台跑完整一轮，并把 `dist/` 自动提交到 GitHub。

Windows / Linux / macOS 同一份代码；Windows 上双击根目录的 `run.bat` 即可。

做四件事：
  1. 记录本机出口位置（ip-api）→ `dist/runner-geo.json`
  2. 跑 `main.py`：采集 → 去重 → TCP 预筛 → 延迟 → 下载测速 → 纯净度 → 出产物
  3. `scripts/verify_dist.py` 校验产物（名字不重复、能加载）
  4. `git add dist` → commit → push（默认做；`--no-publish` 可关）

为什么不用 bash 脚本：原 `run_local.sh` 依赖 flock / date / curl，Windows 上
要么装 Git Bash 要么重写一遍。这里全部用 Python 标准库，两端行为一致。

用法：
    python scripts/run_local.py
    python scripts/run_local.py --max-nodes 40 --no-publish
    python scripts/run_local.py -- --sources manifest --no-speedtest

Windows 首次使用建议直接双击 `run.bat`（会自动建 .venv 并装依赖）。
上传凭据优先读 `GITHUB_TOKEN` / `GH_TOKEN`；没给就走 Git 凭据管理器。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
GEO_URL = ("http://ip-api.com/json/?fields="
           "status,country,countryCode,regionName,city,isp,query")

_SECRET_PATTERNS = (
    re.compile(r"(x-access-token:)([^@\s]+)(@)", re.IGNORECASE),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),
)


def mask_secrets(text: str) -> str:
    """日志里永远不出现 token。git 的报错会把整个 URL 打出来。"""
    text = _SECRET_PATTERNS[0].sub(r"\1***\3", text)
    return _SECRET_PATTERNS[1].sub("***", text)


def build_command(python: str, out: Path, work: Path, max_nodes: int | None,
                  extra: list[str]) -> list[str]:
    """构造 main.py 的命令行。独立成函数是为了能在测试里断言参数接线。"""
    cmd = [python, str(ROOT / "main.py"), "--out", str(out), "--work", str(work)]
    if max_nodes is not None:
        cmd += ["--max-nodes", str(max_nodes)]
    cmd += list(extra)
    return cmd


class _Logger:
    """同时写控制台和 UTF-8 日志文件。

    Windows 的 cmd 默认是 GBK；子进程统一用 UTF-8 输出（见 _run 的 env），
    这里对控制台做 errors="replace"，保证不会因为一个字符崩掉整轮任务。
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("w", encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass

    def __call__(self, message: str = "") -> None:
        line = str(message)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 服务器脚本会 git pull；本地不主动 pull，避免把用户未提交的改动 rebase 掉
    env.setdefault("GIT_TERMINAL_PROMPT", "1")
    return env


def _display(cmd: list[str]) -> str:
    return mask_secrets(" ".join(str(part) for part in cmd))


def _run(cmd: list[str], *, log, cwd: Path = ROOT, env: dict | None = None) -> int:
    log("$ " + _display(cmd))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env or _child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        log(f"[失败] 启动失败：{exc}")
        return 127
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip("\r\n"))
    return proc.wait()


def _capture(cmd: list[str], *, cwd: Path = ROOT) -> str | None:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return None
    return (proc.stdout or "").strip() if proc.returncode == 0 else None


# ---------------------------------------------------------------- 单实例锁

class AlreadyRunning(RuntimeError):
    pass


def _acquire_lock(path: Path):
    """跨平台文件锁：Windows 用 msvcrt，POSIX 用 flock。返回需要 close 的句柄。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if IS_WIN:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("lock")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(str(exc)) from exc
    return handle


def _release_lock(handle, path: Path) -> None:
    try:
        if IS_WIN:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:  # noqa: BLE001
        pass
    finally:
        handle.close()
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 各步骤

def record_geo(out_dir: Path, log) -> None:
    """写本机出口位置。查不到就跳过 —— 产物头部只是少一行，不影响构建。"""
    target = out_dir / "runner-geo.json"
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(GEO_URL, headers={"User-Agent": "clash-daily/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 拿不到出口位置（不影响出产物）：{exc}")
        return
    if not isinstance(data, dict) or data.get("status") != "success":
        log("[warn] ip-api 没有返回可用结果，本轮不写测速位置")
        return
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log(f"出口位置：{data.get('country', '?')}·{data.get('city', '?')} "
        f"({data.get('query', '?')})")


def _check_git_identity(git: str, log) -> bool:
    name = _capture([git, "config", "user.name"])
    email = _capture([git, "config", "user.email"])
    if name and email:
        return True
    log("[失败] git 还没有配置提交身份，先执行一次：")
    log('  git config --global user.name "你的名字"')
    log('  git config --global user.email "you@example.com"')
    return False


def publish(remote: str, log, *, dry_run: bool = False) -> int:
    """提交 dist/ 并 push。返回 0 成功，1 失败。"""
    git = shutil.which("git") or ("git.exe" if IS_WIN else "git")
    if _capture([git, "--version"]) is None:
        log("[失败] 找不到 git，请安装 Git for Windows 并加入 PATH")
        return 1
    if not _check_git_identity(git, log):
        return 1
    if dry_run:
        log(f"$ {git} add dist && {git} commit -m ... && {git} push {remote} HEAD")
        return 0

    _run([git, "add", "dist"], log=log)
    if _capture([git, "diff", "--cached", "--quiet"]) is not None:
        # --quiet 返回 0 = 暂存区和 HEAD 一样，没有可提交的东西
        log("dist/ 没有变化，跳过提交")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    _run([git, "commit", "-m", f"chore(dist): {stamp} 更新订阅"], log=log)

    url = _capture([git, "remote", "get-url", remote]) or ""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    target = remote
    if token and url.startswith("https://") and "github.com" in url:
        # token 只放在这次 push 的参数里，不写进 .git/config
        target = url.replace("https://", f"https://x-access-token:{token}@", 1)
    rc = _run([git, "push", target, "HEAD"], log=log)
    if rc != 0:
        log("[失败] push 没成功。设置 GITHUB_TOKEN 后重试，"
            "或确认 Git 凭据管理器已登录 GitHub。")
        return 1
    log("已上传 GitHub")
    return 0


def _prune_logs(log_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(log_dir.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 入口

def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="跑一轮节点聚合，并把 dist/ 自动上传到 GitHub")
    parser.add_argument("--out", default="dist", help="产物目录（默认 dist）")
    parser.add_argument("--work", default="work", help="内核与临时文件目录（默认 work）")
    parser.add_argument("--max-nodes", type=int, default=None,
                        help="最终保留多少节点（默认交给 main.py，即 80）")
    parser.add_argument("--no-publish", action="store_true", help="只跑，不提交/推送")
    parser.add_argument("--pull", action="store_true", help="跑之前先 git pull --ff-only")
    parser.add_argument("--remote", default="origin", help="推送到哪个远端（默认 origin）")
    parser.add_argument("--log-keep", type=int, default=30, help="保留最近多少份日志")
    parser.add_argument("--dry-run", action="store_true", help="只打印要执行的命令")
    parser.add_argument("extra", nargs=argparse.REMAINDER,
                        help="`--` 之后的参数原样传给 main.py")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    out_dir = Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out
    work_dir = Path(args.work) if Path(args.work).is_absolute() else ROOT / args.work
    extra = list(args.extra)
    if extra and extra[0] == "--":
        extra = extra[1:]

    cmd = build_command(sys.executable, out_dir, work_dir, args.max_nodes, extra)

    if args.dry_run:
        print("main:", _display(cmd))
        print("verify:", _display([sys.executable, str(ROOT / "scripts" / "verify_dist.py"),
                                   "--path", str(out_dir / "clash.yaml")]))
        if not args.no_publish:
            publish(args.remote, print, dry_run=True)
        return 0

    log_dir = ROOT / "logs"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger = _Logger(log_dir / f"run-{stamp}.log")
    log = logger
    lock_path = work_dir / ".run.lock"
    lock = None
    code = 0
    started = time.time()
    try:
        try:
            lock = _acquire_lock(lock_path)
        except AlreadyRunning:
            log("[跳过] 已经有一轮在跑（work/.run.lock 被占用）")
            return 2

        log(f"== {datetime.now():%Y-%m-%d %H:%M:%S} 开始（日志 {logger.path}）")
        log(f"Python {sys.version.split()[0]} / {sys.platform}")

        if args.pull:
            git = shutil.which("git") or "git"
            if _run([git, "pull", "--ff-only"], log=log) != 0:
                log("[warn] git pull 失败，继续用本地代码跑")

        record_geo(out_dir, log)

        if _run(cmd, log=log) != 0:
            log("[失败] main.py 没有正常结束，本轮不出产物")
            return 1

        verify = [sys.executable, str(ROOT / "scripts" / "verify_dist.py"),
                  "--path", str(out_dir / "clash.yaml")]
        if _run(verify, log=log) != 0:
            log("[失败] 产物校验没通过，已保留现场，不推送")
            return 1

        if args.no_publish:
            log("--no-publish：跳过 GitHub 上传")
        else:
            if publish(args.remote, log) != 0:
                log("[失败] 产物是好的，但没能上传 GitHub")
                return 3

        log(f"== 完成，用时 {time.time() - started:.0f}s")
        return 0
    finally:
        if lock is not None:
            _release_lock(lock, lock_path)
        _prune_logs(log_dir, args.log_keep)
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
