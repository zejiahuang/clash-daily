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
#   2. GitHub 的 speedtest 工作流按 cron 轮询这个文件，发现新的 run_id 就测速，
#      把 dist/speed.json 提交回 GitHub 自己的 main（用它自带的 GITHUB_TOKEN）；
#   3. 本脚本在这里轮询 GitHub 的 raw，等到 run_id 对得上就收工。
#
# 代价是「触发」变成了 GitHub 侧的轮询，延迟最多一个 cron 周期（见
# .github/workflows/speedtest.yml 的 schedule）。换来的是全流程零密钥。
#
# ⚠️ 轮询**必须比对 run_id**，不能只看文件存在：speed.json 是提交进仓库的文件，
#    上一轮那份会一直在。只判存在的话，GitHub 没跑起来时这里会安静地拿到旧结果。
set -euo pipefail

CANDIDATES="${1:-dist/candidates.json}"
SPEED_OUT="${SPEED_OUT:-dist/speed.json}"
REPO="${GITHUB_REPO:-zejiahuang/clash-daily}"
BRANCH="${SPEED_BRANCH:-main}"
TIMEOUT_MIN="${WAIT_TIMEOUT_MIN:-90}"
INTERVAL="${WAIT_INTERVAL:-20}"
TIMEOUT_S="${WAIT_TIMEOUT_S:-$((TIMEOUT_MIN * 60))}"
# 可以覆盖，测试时指向本地 HTTP 服务（见 tests/test_handoff.py）
RAW_BASE="${WAIT_RAW_BASE:-https://raw.githubusercontent.com/${REPO}/${BRANCH}}"

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
echo "  （GitHub 侧按 cron 轮询 CNB 上的 candidates.json，最多一个周期后开始）"

mkdir -p "$(dirname "$SPEED_OUT")"
DEADLINE=$(( $(date +%s) + TIMEOUT_S ))
TRIES=0
while :; do
  TRIES=$((TRIES + 1))
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
      echo "收到本轮结果：$SPEED_OUT（第 ${TRIES} 次轮询）"
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
    [ -n "$GOT" ] && echo "  [第 ${TRIES} 次] speed.json 还是上一轮的（run_id=$GOT），继续等"
  else
    [ $((TRIES % 4)) -eq 0 ] && echo "  [第 ${TRIES} 次] 还没有 speed.json"
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "等了 ${TIMEOUT_S} 秒仍没拿到本轮结果（run_id=${RUN_ID}）" >&2
    echo "按这个顺序查：" >&2
    echo "  1) https://github.com/${REPO}/actions 里 speedtest 工作流最近有没有跑、红在哪一步" >&2
    echo "  2) 它的日志里有没有「候选 run_id 和上次一样，跳过」—— 有就说明它读到的是旧候选" >&2
    echo "  3) CNB 这边 dist/candidates.json 有没有真的提交上去（GitHub 只能读已提交的文件）" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
