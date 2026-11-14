@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem Windows 一键入口：首次运行自动建 .venv、装依赖，然后交给 scripts\run_local.py。
rem 之后每次运行只做：跑一轮 -> 校验 dist -> 提交并 push 到 GitHub。

set "VPY=%CD%\.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [setup] 首次运行：创建虚拟环境并安装依赖，请稍候...
  set "BASE="
  where py >nul 2>nul && set "BASE=py -3"
  if not defined BASE (
    where python >nul 2>nul && set "BASE=python"
  )
  if not defined BASE goto :nopython
  %BASE% -m venv .venv
  if errorlevel 1 goto :setupfail
  "%VPY%" -m pip install --upgrade pip >nul
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 goto :setupfail
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%VPY%" scripts\run_local.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [失败] 退出码 %RC%，详情见 logs\ 目录。
  pause
)
exit /b %RC%

:nopython
echo 找不到 Python。请安装 Python 3.11+，安装时勾选 "Add python.exe to PATH"。
pause
exit /b 1

:setupfail
echo 初始化失败（创建虚拟环境或安装依赖出错），请检查网络后重试。
pause
exit /b 1
