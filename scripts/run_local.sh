#!/usr/bin/env bash
# 单机跑完整一轮：采集 → 去重 → TCP 预筛 → 延迟测速 → 下载测速 → 出产物 →（可选）回写仓库。
#
# 和 .cnb.yml 的那三条流水线不同，这里**不拆分**：
#   - 延迟测速和下载测速都在这台机器上做，所以产物头部的「测速位置」只有一个，
#     就是这台机器的出口位置 —— 订阅里的速度对使用者就是这个视角的实测值。
#   - 不再需要 GitHub 的那一段（workflow_dispatch / wait_speed / speed.json 回传）。
#
# 用法：
#   bash scripts/run_local.sh                 # 跑一轮，产物在 dist/
#   PUBLISH=0 bash scripts/run_local.sh       # 只跑，不提交回仓库
#   EXTRA_ARGS="--sources manifest" bash scripts/run_local.sh
#
# 可调环境变量（都有默认值，和 .cnb.yml 里的同名变量一致）：
#   MAX_NODES=80  COUNTRY_SHARE=0.2  TOP_PER_COUNTRY=0  FRAUD_TOP=80
#   SPEED_MAX=0   SPEED_WORKERS=32   SPEED_BYTES=6000000  SPEED_TIMEOUT=10
#   REFINE_TOP=300 REFINE_WORKERS=16 REFINE_BYTES=24000000
#   DELAY_WORKERS=32 DELAY_TIMEOUT=5000 IP_TIMEOUT=6
#   PULL=1（先 git pull --rebase）  PUBLISH=1（跑完提交 dist/ 并 push）
#   LOG_KEEP=30（保留最近多少份日志）  EXTRA_ARGS（原样追加给 main.py）
#
# 退出码：0 = 产物正常（发布失败也算失败，见末尾）；非 0 = 这一轮没产出可用产物。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 产物头部和日志时间都用北京时间。服务器本身常常是 UTC，不写死就会差 8 小时。
export TZ=Asia/Shanghai
# 非交互运行（systemd / cron）里 git 不能停下来问账号密码 —— 会一直挂着不返回。
export GIT_TERMINAL_PROMPT=0

LOG_DIR="${LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"

exec > >(tee -a "$LOG") 2>&1

log() { echo "[$(date '+%F %T')] $*"; }

# 同一时间只允许一轮：上一轮还没跑完就再起一轮，两边会抢 work/ 里的内核和配置。
# ⚠️ 没有 flock 时必须照跑，不能因为「上锁失败」就当成「已有一轮在跑」静默退出 0
#    —— 那会表现成「定时任务天天成功、产物永远不更新」。
mkdir -p "$ROOT/work"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$ROOT/work/.run.lock"
  if ! flock -n 9; then
    log "已有一轮在跑（work/.run.lock 被占用），本次退出"
    exit 0
  fi
else
  log "[warn] 没有 flock，跳过并发保护（不要同时手动跑和定时跑）"
fi

log "===== clash-daily 单机一轮开始 ====="
log "仓库：$ROOT"
log "日志：$LOG"

# ---------- 1. 环境 ----------
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="$(command -v python3 || command -v python || true)"
  fi
fi
if [ -z "$PY" ]; then
  log "[失败] 找不到 python3，先跑 deploy/install.sh"
  exit 1
fi
if ! "$PY" -c 'import yaml' 2>/dev/null; then
  log "[失败] $PY 缺少 PyYAML，先跑 deploy/install.sh（或 pip install -r requirements.txt）"
  exit 1
fi
log "解释器：$PY（$("$PY" -V 2>&1)）"

# ---------- 2. 同步代码（可选）----------
# 产物是提交回 main 的，本地落后就会在 push 时被拒；先 rebase 一次最省事。
if [ "${PULL:-1}" = "1" ] && git rev-parse --git-dir >/dev/null 2>&1; then
  if git remote get-url origin >/dev/null 2>&1; then
    log "git pull --rebase --autostash origin main"
    git pull --rebase --autostash origin main || log "[warn] 同步失败，继续用本地代码跑"
  fi
fi

# ---------- 3. 记录测速位置 ----------
# 速度是「这台机器 → 节点」的实测值，不写清楚使用者会当成自己那边的速度。
# 拿不到就删掉旧文件，绝不能沿用上一轮的（那会写出一个错的测速位置）。
rm -f dist/runner-geo.json
mkdir -p dist
# ip-api 的空查询直接返回「我自己是谁」，一次请求就拿到 IP + 归属地。
# 它不通时退回 ifconfig.me + 按 IP 查 —— 单点失败不该让产物头部少一行。
FIELDS="status,country,countryCode,regionName,city,isp,query"
GEO="$(curl -s --max-time 10 "http://ip-api.com/json/?fields=${FIELDS}" || true)"
case "$GEO" in
  *'"status":"success"'*)
    IP="$(printf '%s' "$GEO" | grep -o '"query":"[^"]*"' | cut -d'"' -f4)" ;;
  *) IP=""; GEO="" ;;
esac
if [ -z "$IP" ]; then
  IP="$(curl -s --max-time 10 https://ifconfig.me/ip || true)"
  if [ -n "$IP" ]; then
    GEO="$(curl -s --max-time 10 \
      "http://ip-api.com/json/${IP}?fields=status,country,countryCode,regionName,city,isp" \
      || true)"
  fi
fi
if [ -n "$IP" ]; then
  [ -n "$GEO" ] || GEO='{}'
  log "出口 IP：$IP  $GEO"
  printf '%s\n' "$GEO" > dist/runner-geo.json
else
  log "[warn] 拿不到出口 IP，本轮不写测速位置"
fi

# ---------- 4. 主流程（测速也在这台机器上）----------
log "main.py --stage all"
# shellcheck disable=SC2086
"$PY" main.py \
  --stage all \
  --out dist \
  --work work \
  --max-nodes "${MAX_NODES:-80}" \
  --country-share "${COUNTRY_SHARE:-0.2}" \
  --top-per-country "${TOP_PER_COUNTRY:-0}" \
  --fraud-top "${FRAUD_TOP:-80}" \
  --speed-max "${SPEED_MAX:-0}" \
  --speed-workers "${SPEED_WORKERS:-32}" \
  --speed-bytes "${SPEED_BYTES:-6000000}" \
  --speed-timeout "${SPEED_TIMEOUT:-10}" \
  --refine-top "${REFINE_TOP:-300}" \
  --refine-workers "${REFINE_WORKERS:-16}" \
  --refine-bytes "${REFINE_BYTES:-24000000}" \
  --delay-workers "${DELAY_WORKERS:-32}" \
  --delay-timeout "${DELAY_TIMEOUT:-5000}" \
  --ip-timeout "${IP_TIMEOUT:-6}" \
  ${SOURCES:+--sources "$SOURCES"} \
  ${EXTRA_ARGS:-}

# ---------- 5. 校验产物 ----------
"$PY" scripts/verify_dist.py --path dist/clash.yaml
log "产物校验通过"

# ---------- 6. 回写仓库（可选）----------
if [ "${PUBLISH:-1}" = "1" ]; then
  log "提交 dist/ 并推送"
  if bash scripts/ci_commit.sh \
      "chore(nodes): 更新 $(date +'%Y-%m-%d %H:%M') 北京时间（单机）"; then
    log "已推送"
  else
    log "[失败] 产物已生成在 dist/，但没能推送到远端（订阅地址不会更新）"
    exit 1
  fi
else
  log "PUBLISH=0，跳过回写（产物只在 $ROOT/dist）"
fi

# ---------- 7. 清理旧日志 ----------
find "$LOG_DIR" -name 'run-*.log' -type f -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | tail -n "+$(( ${LOG_KEEP:-30} + 1 ))" | cut -d' ' -f2- \
  | while read -r old; do rm -f "$old"; done || true

log "===== 完成 ====="
log "订阅文件：$ROOT/dist/clash.yaml"
log "Base64：  $ROOT/dist/clash.base64.txt"
log "报告：    $ROOT/dist/report.md"
