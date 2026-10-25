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
#   2. GitHub 的 speedtest 工作流**常驻看守**（一个 job 活 5.5 小时），每 30 秒看
#      一眼这个文件，发现新的 run_id 就测速，把 dist/speed.json 提交回它自己的 main；
#   3. 本脚本在这里轮询 GitHub 的 raw，等到 run_id 对得上就收工。
#
# ⚠️ **「GitHub 按 cron 每 10 分钟跑一次」这个前提是错的。** 2026-10-25 实测：公开
#    仓库的 `schedule` 每 **117~148 分钟**才触发一次（6 次运行全量对照）。于是
#    「最多等一个 cron 周期」等的东西根本不存在 —— 那天这里老老实实等了 40 分钟，
#    打了 119 行「还是上一轮的」，而 GitHub 一次都没被叫醒。现在 GitHub 侧改成常驻
#    看守，触发延迟是「最长 30 秒」；本脚本剩下的职责是**等结果**，不是等触发。
#
# ⚠️ 轮询**必须比对 run_id**，不能只看文件存在：speed.json 是提交进仓库的文件，
#    上一轮那份会一直在。只判存在的话，GitHub 没跑起来时这里会安静地拿到旧结果。
#
# ⚠️ **等不到时要能说清「为什么」**。判据只有一条：GitHub Actions 现在到底有没有
#    在跑 —— 这个可以匿名读公开仓库的 API，不需要令牌。有了它，「没人叫它」和
#    「叫了但跳过了」才分得开；没有它，日志就只是一百多行一模一样的话。
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
# 「最近一次运行都过去这么久了、而且现在没有在跑」⇒ 它根本没被触发，再等没意义
GH_IDLE_MIN="${WAIT_GH_IDLE_MIN:-180}"
GH_BAD_LIMIT="${WAIT_GH_BAD_LIMIT:-2}"

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
echo "  GitHub 侧是常驻看守（每 30 秒看一眼 CNB），正常情况下 30 秒内就开始测"
echo "  最多等 ${TIMEOUT_MIN} 分钟；如果它根本没在跑，这里会提前放弃并说明原因"

TMP_GH="$(mktemp)"
trap 'rm -f "$TMP_GH"' EXIT

# 打印一行「GitHub 那边现在什么状态」。退出码：
#   0 = 有在跑 / 刚跑过（继续等）
#   1 = 很久没跑且现在没有在跑（等下去没有意义）
#   2 = 查不到（限流 / 不可达，不能据此判断任何事）
gh_probe() {
  local body
  if ! body="$(curl -fsS --max-time 15 -H 'Accept: application/vnd.github+json' \
        "$GH_API" 2>/dev/null)"; then
    echo "GitHub：查不到 Actions 状态（API 不可达或被限流）"
    return 2
  fi
  printf '%s' "$body" > "$TMP_GH"
  python - "$TMP_GH" "$GH_IDLE_MIN" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
idle_min = float(sys.argv[2])


def parse(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


try:
    runs = json.load(open(sys.argv[1], encoding="utf-8")).get("workflow_runs") or []
except Exception:
    print("GitHub：Actions 状态解析失败")
    raise SystemExit(2)

if not runs:
    print("GitHub：speedtest 工作流一次都没跑过 —— 它没被触发过，等下去没有意义")
    raise SystemExit(1)

now = datetime.now(timezone.utc)
busy = [r for r in runs if r.get("status") in ("in_progress", "queued")]
if busy:
    run = busy[0]
    started = parse(run["created_at"])
    print(f"GitHub：看守在跑（{run['status']}，{started.astimezone(CST):%H:%M} 开始，"
          f"已 {(now - started).total_seconds() / 60:.0f} 分钟）")
    raise SystemExit(0)

newest = max(runs, key=lambda r: r.get("created_at") or "")
started = parse(newest["created_at"])
age = (now - started).total_seconds() / 60
print(f"GitHub：现在没有在跑；最近一次 {started.astimezone(CST):%H:%M} 开始"
      f"（{age:.0f} 分钟前，{newest.get('conclusion') or newest.get('status')}）")
raise SystemExit(0 if age < idle_min else 1)
PY
}

elapsed_fmt() {
  printf '%d:%02d' $(( $1 / 60 )) $(( $1 % 60 ))
}

mkdir -p "$(dirname "$SPEED_OUT")"
START=$(date +%s)
DEADLINE=$((START + TIMEOUT_S))
TRIES=0
GH_BAD=0
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
    if [ "$RC" -eq 1 ]; then GH_BAD=$((GH_BAD + 1)); else GH_BAD=0; fi
    if [ "$GH_BAD" -ge "$GH_BAD_LIMIT" ]; then
      {
        echo "GitHub 那边连续 ${GH_BAD} 次判定「根本没在跑」—— 再等下去不会有人来测"
        echo "处理办法（都不需要令牌）："
        echo "  1) 打开 https://github.com/${REPO}/actions/workflows/speedtest.yml"
        echo "     点 Run workflow；看守起来后这里 30 秒内就会接上"
        echo "  2) 或者重跑 CNB 这一轮，等下一次看守起来"
      } >&2
      exit 1
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
