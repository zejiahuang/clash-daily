"""挑一个**原生**的 bash —— 本机 PATH 里的 `bash` 很可能是 WSL 那个。

⚠️ 为什么需要这个（这不是洁癖，是两次误报的来源）：
   Windows 上 `C:\\Windows\\System32\\bash.exe`（WSL 的入口）排在 Git 自带的
   bash 前面，两者行为差两处，而且两处都会伪装成「脚本坏了」：

   1. **不继承 Windows 环境变量** —— `CI_FORCE=1` 传不进去，于是
      「发布候选要穿过防自触发守卫」那条测试会报「CI_FORCE=1 时仍失败」；
   2. **不认 `D:\\...` 路径** —— 报
      `D:clash-dailyscriptswait_speed.sh: No such file or directory`
      （反斜杠被吃掉），看着像文件不存在。

   而脚本本身在 CNB（Linux）上一直是好的 —— 错的是解释器。

Linux / CNB 上没有前两个候选，直接落到 `shutil.which("bash")`。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_CANDIDATES = (
    os.environ.get("BASH_BIN", ""),
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def find_bash() -> str:
    """返回一个原生 bash 的可执行路径；找不到就退回 PATH 里的 `bash`。"""
    for cand in _CANDIDATES:
        if cand and Path(cand).is_file():
            return cand
    return shutil.which("bash") or "bash"
