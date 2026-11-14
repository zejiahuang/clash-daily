#!/usr/bin/env python3
"""单机部署的接线自检：`scripts/run_local.sh` 传给 main.py 的参数必须真的存在。

为什么值得单独一个测试：这条脚本是**唯一**不经过 `.cnb.yml` 的入口，改 main.py
的参数名时不会有任何东西提醒你 —— 表现是「定时任务天天红，日志里一行
unrecognized arguments」，而那时已经是凌晨两点、人不在电脑前。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_local.sh"
INSTALL = ROOT / "deploy" / "install.sh"


def _help_text() -> str:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--help"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, f"main.py --help 失败：{proc.stderr[-500:]}"
    return proc.stdout


def main() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    help_text = _help_text()

    # 只取「真正喂给 main.py 的那一段」，否则 curl 的 --max-time、find 的 --name
    # 都会被当成 main.py 的参数，测试就变成了误报制造机。
    assert '"$PY" main.py' in script, "run_local.sh 里找不到 main.py 调用"
    block = script.split('"$PY" main.py', 1)[1].split("# ---------- 5.", 1)[0]
    flags = set(re.findall(r"(--[a-z][a-z0-9-]+)", block))

    missing = sorted(f for f in flags if f not in help_text)
    assert not missing, f"main.py 不认这些参数了：{missing}"

    # 单机一条龙必须显式 --stage all：默认值虽然是 all，但写出来才能防止
    # 以后有人把默认值改成 delay 之后「定时任务只跑了一半」却毫无提示。
    assert "--stage all" in block, "run_local.sh 必须显式 --stage all"

    # 时区：crontab / systemd 按服务器本地时区触发，日志和产物头部必须固定北京时间。
    assert "export TZ=Asia/Shanghai" in script, "run_local.sh 必须固定 TZ=Asia/Shanghai"

    # 非交互运行时 git 不能停下来等账号密码
    assert "GIT_TERMINAL_PROMPT=0" in script, "run_local.sh 必须关闭 git 的交互式认证"

    # 并发锁：没有 flock 时必须照跑（否则「锁命令不存在」= 静默退出 0）
    assert "command -v flock" in script, "flock 不存在时不能让整轮静默退出 0"

    # 失败要能传导出去：这几条命令任一失败都必须让整轮非 0 退出
    assert "set -euo pipefail" in script, "run_local.sh 必须 set -euo pipefail"

    # 安装器：两个 unit 都要生成，且 timer 用 OnCalendar（Persistent 保证错过补跑）
    assert "${SERVICE_NAME}.service" in install and "${SERVICE_NAME}.timer" in install, \
        "install.sh 必须同时写 service 和 timer"
    assert "OnCalendar=" in install and "Persistent=true" in install, \
        "timer 必须用 OnCalendar + Persistent=true"
    assert "PUBLISH=1" in install, "service 里要显式写 PUBLISH（不想回写改成 0）"

    print("✓ 单机部署接线通过：参数与 main.py 一致、阶段/时区/锁/防挂起齐全、"
          "systemd 的 service+timer 都写了")


if __name__ == "__main__":
    main()
