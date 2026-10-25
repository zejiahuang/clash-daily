#!/usr/bin/env bash
# 叫醒 GitHub 的 speedtest 工作流 —— 由 CNB 主动触发，不再靠 GitHub 的 cron 轮询。
#
# 为什么要这么改：GitHub 公开仓库的 `schedule` 实测每 117~148 分钟才触发一次
# （2026-10-25，6 次运行全量对照），靠它叫醒等于没有。以前为了「零密钥」，只能让
# 一个 job 常驻 5.5 小时轮询 —— 那个形状在 Actions 页面上看起来像卡死，已经被误取消
# 过一次（run 40155272778，活了 15 分 19 秒），而且每轮都要重装一遍环境。
# 现在 CNB 手里有令牌，直接 POST `workflow_dispatch`，秒级，任务跑完就退出。
#
# ⚠️ **令牌绝不写进本仓库**。这个仓库是**公开**的：写进去等于把写权限发给所有人，
#    而且 GitHub 的密钥扫描会把公开仓库里出现的 `ghp_` 令牌**自动吊销** ——
#    那不是「不安全但能用」，是「过几小时就坏」。
#    正确做法是 CNB 的「密钥仓库」（仓库类型选 `密钥仓库`，只能网页编辑、不能 clone），
#    `.cnb.yml` 用 `imports:` 把里面的键注入成环境变量。本脚本只读环境变量。
#
# 用法：bash scripts/gh_dispatch.sh
#   GH_DISPATCH_TOKEN   必需（来自 imports）；**为空时只警告、不失败** —— 见下
#   GITHUB_REPO / GH_WORKFLOW / GH_REF  可覆盖
#
# 为什么没令牌时不 exit 1：刚建好流水线、密钥仓库还没配的时候，不该让整条流水线红。
# 这时退回 GitHub 自己的 cron 兜底（慢，但能通），并且把「怎么配」打在日志里。
set -euo pipefail

REPO="${GITHUB_REPO:-zejiahuang/clash-daily}"
WORKFLOW="${GH_WORKFLOW:-speedtest.yml}"
REF="${GH_REF:-main}"
API="https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches"

if [ -z "${GH_DISPATCH_TOKEN:-}" ]; then
  echo "⚠️ 没有 GH_DISPATCH_TOKEN —— 没法主动叫醒 GitHub"
  echo "   配法（一次性，约 1 分钟）："
  echo "     1) https://cnb.cool/new/repos 建仓库，**仓库类型选「密钥仓库」**，名字 clash-daily-secrets"
  echo "     2) 在它的网页里建 envs.yml，内容一行：GH_DISPATCH_TOKEN: <GitHub 令牌>"
  echo "     3) 把 .cnb.yml 里 imports: 那三行的注释去掉（文件里有说明）"
  echo "   现在退回 GitHub 自己的 cron 兜底：实测 117~148 分钟才触发一次。"
  exit 0
fi

echo "叫醒 GitHub 测速：${REPO} · ${WORKFLOW} @ ${REF}"

# 令牌只出现在这个 header 里，脚本不开 -x，所以不会被日志打出来。
code=$(curl -sS --max-time 30 -o /tmp/dispatch.out -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer ${GH_DISPATCH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  "$API" -d "{\"ref\":\"${REF}\"}" 2>/dev/null || echo 000)

case "$code" in
  204)
    echo "已触发（HTTP 204）。GitHub 那边通常 10 秒内开始排队。"
    exit 0
    ;;
  401)
    echo "✗ 令牌无效或已被吊销（HTTP 401）" >&2
    echo "  ⇒ 去 GitHub → Settings → Developer settings 重新生成，然后更新密钥仓库里的 envs.yml" >&2
    ;;
  403)
    echo "✗ 令牌没权限（HTTP 403）" >&2
    echo "  ⇒ 它至少需要对这个仓库的 Actions: write（经典令牌要 workflow scope）" >&2
    ;;
  404)
    echo "✗ 找不到工作流或仓库（HTTP 404）：${REPO} / ${WORKFLOW}" >&2
    echo "  ⇒ 也可能是令牌看不到这个仓库（细粒度令牌要显式勾选该仓库）" >&2
    ;;
  422)
    echo "✗ 请求被拒（HTTP 422）—— 通常是 ref「${REF}」不存在" >&2
    ;;
  000)
    echo "✗ 连不上 api.github.com —— 不是令牌问题，是 CNB 到 GitHub 的网络" >&2
    ;;
  *)
    echo "✗ 触发失败：HTTP ${code}" >&2
    head -c 400 /tmp/dispatch.out >&2 2>/dev/null || true
    echo >&2
    ;;
esac

rm -f /tmp/dispatch.out
exit 1
