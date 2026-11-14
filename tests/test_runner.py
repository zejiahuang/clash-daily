"""跨平台运行脚本的接线测试（Windows 上同样跑这一份）。

重点不是「脚本能不能启动」，而是两件容易静默出错的事：
  * `--max-nodes` / `--` 之后的参数有没有真的传到 main.py
  * token 会不会被 git 的报错带进日志
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_local  # noqa: E402


def main() -> int:
    problems: list[str] = []

    out = Path("/tmp/dist")
    work = Path("/tmp/work")
    cmd = run_local.build_command("py", out, work, 40,
                                  ["--sources", "manifest", "--no-speedtest"])
    if cmd[1] != str(ROOT / "main.py"):
        problems.append(f"第一条命令必须是 main.py，实得 {cmd[1]}")
    joined = " ".join(cmd)
    for expected in ("--out /tmp/dist", "--work /tmp/work",
                     "--max-nodes 40", "--sources manifest", "--no-speedtest"):
        if expected not in joined:
            problems.append(f"命令行缺少 {expected!r}：{joined}")

    no_limit = run_local.build_command("py", out, work, None, [])
    if "--max-nodes" in no_limit:
        problems.append("没指定 --max-nodes 时不应自己塞一个默认值")

    sample = ("https://x-access-token:ghp_"
              "abcdefghijklmnopqrstuvwxyz0123456789@github.com/a/b.git")
    masked = run_local.mask_secrets(sample)
    if "ghp_" in masked or "abcdefghijklmnop" in masked:
        problems.append(f"token 没被遮住：{masked}")
    if "***" not in masked or "github.com" not in masked:
        problems.append(f"遮罩后应保留 URL 结构：{masked}")

    # --dry-run 必须不碰网络、不加锁、不写 dist，只打印计划
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_local.py"),
         "--dry-run", "--no-publish", "--max-nodes", "5"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        problems.append(f"--dry-run 退出码 {proc.returncode}：{proc.stderr[-300:]}")
    if "main.py" not in proc.stdout or "verify_dist.py" not in proc.stdout:
        problems.append("--dry-run 没有打印出 main.py / verify_dist.py 两步")
    if "--max-nodes 5" not in proc.stdout:
        problems.append("--dry-run 输出的命令里没有 --max-nodes 5")

    # 单实例锁：第二轮必须立刻被拒绝，而不是两个进程一起抢端口
    import tempfile
    lock_path = Path(tempfile.mkdtemp()) / ".run.lock"
    handle = run_local._acquire_lock(lock_path)
    try:
        try:
            run_local._acquire_lock(lock_path)
        except run_local.AlreadyRunning:
            pass
        else:
            problems.append("第二轮没有被文件锁拦住")
    finally:
        run_local._release_lock(handle, lock_path)
    if lock_path.exists():
        problems.append("锁文件在释放后应被删除")
    probe = run_local._acquire_lock(lock_path)
    run_local._release_lock(probe, lock_path)

    if problems:
        print(f"✗ {len(problems)} 处失败")
        for item in problems:
            print("   -", item)
        return 1
    print("✓ 跨平台运行脚本通过：参数透传、--dry-run、token 遮罩都正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
