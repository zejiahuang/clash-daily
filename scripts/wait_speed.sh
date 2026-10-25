#!/usr/bin/env bash
# 等 GitHub 把测速结果交回来。
#
# 用法：bash scripts/wait_speed.sh [dist/candidates.json]
#
# 为什么这里**没有令牌、也不推送**：CNB 仓库是公开的，往公开仓库里写一个 GitHub
# PAT 等于把仓库写权限发给所有人；而且 GitHub 的密钥扫描会把公开仓库里出现的
# `ghp_` 令牌**自动吊销** —— 那种做法不是「不安全但能用」，是「过几小时就坏掉」。
#
# 所以方向反过来：**GitHub 自己来取**。
#   1. CNB 先把 dist/candidates.json 提交上去（公开可读）；
#   2. CNB 用令牌 POST `workflow_dispatch` 把它叫醒（scripts/gh_dispatch.sh），
#      GitHub 那边只跑一轮，把 dist/speed.json 提交回它自己的 main；
#   3. 本脚本在这里轮询 GitHub 的 raw，等到 run_id 对得上就收工。
#
# ⚠️ **「GitHub 按 cron 每 10 分钟跑一次」这个前提是错的。** 2026-10-25 实测：公开
#    仓库的 `schedule` 每 **117~148 分钟**才触发一次（6 次运行全量对照）。于是
#    「最多等一个 cron 周期」等的东西根本不存在 —— 那天这里老老实实等了 40 分钟，
#    打了 119 行「还是上一轮的」，而 GitHub 一次都没被叫醒。
#    中间试过「常驻 5.5 小时的看守」：能work，但在 Actions 页面上就是一行
#    `in_progress` 挂着，看起来像卡死，被误取消过一次。现在 CNB 手里有令牌，
#    直接 dispatch，触发延迟是「秒级」；本脚本的职责是**等结果**，不是等触发。
#
# ⚠️ 轮询**必须比对 run_id**，不能只看文件存在：speed.json 是提交进仓库的文件，
#    上一轮那份会一直在。只判存在的话，GitHub 没跑起来时这里会安静地拿到旧结果。
#
# ⚠️ **等不到时要能说清「为什么」**。判据只有一条：GitHub Actions 现在到底有没有
#    在跑 —— 这个可以匿名读公开仓库的 API，不需要令牌。有了它，「没人叫它」和
#    「叫了但跳过了」才分得开；没有它，日志就只是一百多行一模一样的话。
#
# ⚠️ **「GitHub 那边没有在跑」要当成故障，不能当成「再等等」**。上一步刚刚用令牌
#    dispatch 过，正常情况下几秒内就该有一个 run 在排队；看不到它，说明叫醒失败了
#    （令牌没配、失效、或者 CNB 到 api.github.com 不通）。2026-10-25 就撞上过：
#    那个 job 在 Actions 页面上是一行 `in_progress`、看起来像卡住，被取消之后
#    CNB 这边什么都不知道，还在按 60 分钟上限安静地轮询。现在的规则是：连续
#    WAIT_IDLE_GRACE（默认 15 分钟）没有 run 在跑就放弃，并把「怎么救」打出来 ——
#    与此同时每一行都提醒一次，操作的人不用等失败也能处理。
set -euo pipefail

CANDIDATES="${1:-dist/candidates.json}"
SPEED_OUT="${SPEED_OUT:-dist/speed.json}"
REPO="${GITHUB_REPO:-zejiahuang/clash-daily}"
BRANCH="${SPEED_BRANCH:-main}"
# 看守常驻 + 失败快速放弃，所以上限可以比过去小：正常情况下 30 秒就拿到。
TIMEOUT_MIN="${WAIT_TIMEOUT_MIN:-60}"
INTERVAL="${WAIT_INTERVAL:-20}"
TIMEOUT_S="${WAIT_TIMEOUT_S:-$((TIMEOUT_MIN * 60))}"
# 可以覆盖，测试时指向本地 HTTP 服务（见 tests/test_handoff.py）
RAW_BASE="${WAIT_RAW_BASE:-https://raw.githubusercontent.com/${REPO}/${BRANCH}}"
# 匿名读公开仓库的 Actions 列表；`off` 关掉探测（测试里用）
GH_API="${WAIT_GH_API:-https://api.github.com/repos/${REPO}/actions/workflows/speedtest.yml/runs?per_page=5}"
# 匿名 API 限流 60 次/小时 ⇒ 探测必须节流，默认 3 分钟一次（20 次/小时）
GH_PROBE_EVERY="${WAIT_GH_PROBE_EVERY:-180}"
# 「没有看守在跑」最多容忍多久（秒）。给 15 分钟宽限是留给「cron 恰好在这一刻起来」，
# 超过它就放弃 —— 而不是按 60 分钟上限安静地等两小时。
IDLE_GRACE="${WAIT_IDLE_GRACE:-900}"

elapsed_fmt() {
  printf '%d:%02d' $(( $1 / 60 )) $(( $1 % 60 ))
}

dur_fmt() {   # 秒 -> 「3 分钟」/「45 秒」：日志里给人看的
  if [ "$1" -ge 120 ]; then printf '%d 分钟' $(( $1 / 60 )); else printf '%d 秒' "$1"; fi
}

if [ ! -s "$CANDIDATES" ]; then
  echo "找不到 $CANDIDATES —— 延迟测速那一段没有产出候选" >&2
  exit 1
fi

RUN_ID="$(python - "$CANDIDATES" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("run_id") or "")
PY
)"
if [ -z "$RUN_ID" ]; then
  echo "$CANDIDATES 里没有 run_id —— 没法把结果和这一轮配对" >&2
  exit 1
fi
echo "等 GitHub 测速：run_id=$RUN_ID"
echo "  CNB 上一步已经用令牌叫醒它，正常情况下 30 秒内开始测、3 分钟内出结果"
echo "  最多等 ${TIMEOUT_MIN} 分钟；如果 $(dur_fmt "$IDLE_GRACE") 内一直没有 run 在跑，会提前放弃并说明原因"

TMP_GH="$(mktemp)"
trap 'rm -f "$TMP_GH"' EXIT

# 打印一行「GitHub 那边现在什么状态」。退出码：
#   0 = 有 run 在跑（排队也算）
#   1 = 一个在跑的都没有（不管上一次是什么时候、什么结论）
#   2 = 查不到（限流 / 不可达，不能据此判断任何事）
gh_probe() {
  local body
  if ! body="$(curl -fsS --max-time 15 -H 'Accept: application/vnd.github+json' \
        "$GH_API" 2>/dev/null)"; then
    echo "GitHub：查不到 Actions 状态（API 不可达或被限流）"
    return 2
  fi
  printf '%s' "$body" > "$TMP_GH"
  python - "$TMP_GH" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def parse(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


try:
    runs = json.load(open(sys.argv[1], encoding="utf-8")).get("workflow_runs") or []
except Exception:
    print("GitHub：Actions 状态解析失败")
    raise SystemExit(2)

if not runs:
    print("GitHub：speedtest 工作流一次都没跑过 —— 它从来没被触发过")
    raise SystemExit(1)

now = datetime.now(timezone.utc)
busy = [r for r in runs if r.get("status") in ("in_progress", "queued")]
if busy:
    run = busy[0]
    started = parse(run["created_at"])
    print(f"GitHub：有 run 在跑（{run['status']}，{started.astimezone(CST):%H:%M} 开始，"
          f"已 {(now - started).total_seconds() / 60:.0f} 分钟）")
    raise SystemExit(0)

newest = max(runs, key=lambda r: r.get("created_at") or "")
started = parse(newest["created_at"])
age = (now - started).total_seconds() / 60
print(f"GitHub：现在没有在跑；最近一次 {started.astimezone(CST):%H:%M} 开始"
      f"（{age:.0f} 分钟前，{newest.get('conclusion') or newest.get('status')}）")
raise SystemExit(1)
PY
}

mkdir -p "$(dirname "$SPEED_OUT")"
START=$(date +%s)
DEADLINE=$((START + TIMEOUT_S))
TRIES=0
IDLE_SINCE=0       # 第一次发现「没有看守在跑」的时间戳（0 = 现在有人守着）
NEXT_GH=0          # 下一次问 GitHub 的时间戳（节流用）
NEXT_REPORT=0      # 下一次说「speed.json 还是上一轮的」的时间戳

while :; do
  TRIES=$((TRIES + 1))
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))

  # ---------- 问一次 GitHub 那边在不在跑 ----------
  if [ "$GH_API" != "off" ] && [ "$NOW" -ge "$NEXT_GH" ]; then
    NEXT_GH=$((NOW + GH_PROBE_EVERY))
    RC=0
    STATE="$(gh_probe)" || RC=$?
    echo "  [$(elapsed_fmt "$ELAPSED")] $STATE"
    if [ "$RC" -eq 0 ]; then
      # 有 run 在跑 ⇒ 重新计时（中途被人点起来也要能把宽限清零）
      IDLE_SINCE=0
    elif [ "$RC" -eq 1 ]; then
      if [ "$IDLE_SINCE" -eq 0 ]; then
        IDLE_SINCE=$NOW
      fi
      # 每一行都带上「怎么救」：操作的人不用等失败就能处理
      echo "         ⇒ 上一步「叫醒 GitHub 测速」没成功（多半是令牌没配 / 失效）—— 看那一步的日志"
      echo "           手动救急：https://github.com/${REPO}/actions/workflows/speedtest.yml 点 Run workflow"
      if [ $((NOW - IDLE_SINCE)) -ge "$IDLE_GRACE" ]; then
        {
          echo "GitHub 那边已经 $(dur_fmt $(( NOW - IDLE_SINCE )))没有 run 在跑 —— 再等下去也不会有人来测"
          echo "（兜底的 cron 实测 117~148 分钟才触发一次，靠它自己回来要等两小时）"
          echo "处理办法："
          echo "  1) 先看「叫醒 GitHub 测速」那一步的日志 —— 令牌没配 / 失效是这里最常见的成因"
          echo "  2) 手动救急：打开 https://github.com/${REPO}/actions/workflows/speedtest.yml 点 Run workflow"
          echo "  3) 或者重跑 CNB 这一轮"
        } >&2
        exit 1
      fi
    fi
  fi

  # ---------- 取结果 ----------
  # ?t= 是缓存穿透：raw.githubusercontent 默认 max-age=300，不换 URL 会读到 5 分钟前的。
  URL="${RAW_BASE}/dist/speed.json?t=$(date +%s)${TRIES}"
  if curl -fsS --max-time 30 -H 'Cache-Control: no-cache' "$URL" -o /tmp/speed.json 2>/dev/null; then
    GOT="$(python - /tmp/speed.json <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("run_id") or "")
except Exception:
    print("")
PY
)"
    if [ "$GOT" = "$RUN_ID" ]; then
      cp /tmp/speed.json "$SPEED_OUT"
      echo "收到本轮结果（第 ${TRIES} 次轮询，等了 $(elapsed_fmt "$ELAPSED")）：$SPEED_OUT"
      python - "$SPEED_OUT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
s = d.get("stats") or {}
print(f"  run_id={d.get('run_id')} 可用 {s.get('ok')}/{s.get('total')} "
      f"耗时 {d.get('seconds')}s 位置 {d.get('vantage')}")
for item in s.get("errors") or []:
    print(f"  {item['count']:>4} 个  {item['reason']}")
PY
      exit 0
    fi
    # 每分钟说一次就够 —— 过去这里是每次轮询都说，40 分钟刷出 119 行一样的话，
    # 既看不出进展也看不出卡在哪。
    if [ -n "$GOT" ] && [ "$NOW" -ge "$NEXT_REPORT" ]; then
      NEXT_REPORT=$((NOW + 60))
      echo "  [$(elapsed_fmt "$ELAPSED")] speed.json 还是上一轮的（run_id=$GOT）"
    fi
  fi

  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "等了 ${TIMEOUT_S} 秒仍没拿到本轮结果（run_id=${RUN_ID}）" >&2
    if [ "$GH_API" != "off" ]; then
      RC=0
      STATE="$(gh_probe)" || RC=$?
      echo "  GitHub 最后一次状态：$STATE" >&2
    fi
    echo "按这个顺序查：" >&2
    echo "  1) https://github.com/${REPO}/actions/workflows/speedtest.yml —— 看守有没有在跑" >&2
    echo "  2) 看守的日志里有没有「候选 run_id=… 已经测过 ⇒ 继续等下一轮」" >&2
    echo "     —— 有就说明 speed.json 里已经是本轮的 run_id，只是 raw 缓存还没换" >&2
    echo "  3) 它的日志里有没有「取候选失败」—— 那是 cnb.cool 从 GitHub 取不到" >&2
    echo "  4) CNB 这边 dist/candidates.json 有没有真的提交上去（GitHub 只能读已提交的文件）" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
