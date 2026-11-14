#!/usr/bin/env bash
# 在这台服务器上把 clash-daily 装成「每天自动跑一轮」。
#
#   sudo bash deploy/install.sh                 # 装 systemd 定时任务（默认 02:00 北京时间）
#   sudo RUN_AT=03:30 bash deploy/install.sh    # 换个时间
#   sudo WEB_PORT=8080 bash deploy/install.sh   # 换订阅站点端口（默认 60066）
#   bash deploy/install.sh --no-service         # 只装依赖，不装定时任务
#   bash deploy/install.sh --no-web             # 不装订阅 HTTP 站点
#
# 做的事只有四件：
#   1. 建 .venv 并装 requirements.txt（不污染系统 Python）
#   2. 预下载 mihomo 内核到 work/bin（第一次跑的时候下载也行，失败会重试镜像）
#   3. 写 systemd 的 service + timer（没有 systemd 时打印 crontab 行，不强行改）
#   4. 写订阅站点的 service（dist/ 直挂 HTTP，默认 0.0.0.0:60066）
#
# 产物仍然写在本仓库的 dist/ 下；要不要回写远端由 run_local.sh 的 PUBLISH 决定。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_AT="${RUN_AT:-02:00}"
# ⚠️ 端口必须是 0–65535。66666 这类「看起来像端口」的五位数会直接 bind 失败。
WEB_PORT="${WEB_PORT:-60066}"
SERVICE_NAME="clash-daily"
WEB_SERVICE_NAME="clash-daily-web"
NO_SERVICE=0
NO_WEB=0
for arg in "$@"; do
  case "$arg" in
    --no-service) NO_SERVICE=1 ;;
    --no-web) NO_WEB=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

log() { echo "[install] $*"; }

# ---------- 1. venv + 依赖 ----------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  log "创建 .venv"
  python3 -m venv "$ROOT/.venv"
fi
log "安装依赖"
"$ROOT/.venv/bin/python" -m pip install -q --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/requirements.txt"

log "跑一遍单元测试"
for f in "$ROOT"/tests/test_*.py; do
  "$ROOT/.venv/bin/python" "$f" >/dev/null || { log "测试失败：$f"; exit 1; }
done
log "测试通过"

# ---------- 2. 预下载内核 ----------
# 只是提前把「下载失败」暴露在安装阶段，而不是等凌晨 2 点整轮跑一半才红。
# 已经存在就跳过（ensure_binary 自己也复用）。
log "准备 mihomo 内核"
"$ROOT/.venv/bin/python" - <<'PY'
from pathlib import Path
from clashhunter.mihomo import ensure_binary
ensure_binary(Path("work/bin"), log=lambda m: print(m, flush=True))
PY

if [ "$NO_SERVICE" = "1" ]; then
  log "--no-service：跳过定时任务"
  log "手动跑一轮：bash $ROOT/scripts/run_local.sh"
  exit 0
fi

# ---------- 3. 定时任务 ----------
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  log "没有 systemd，下面这些行加进 crontab -e / 自启脚本即可："
  echo
  echo "0 2 * * * PUBLISH=1 /usr/bin/bash $ROOT/scripts/run_local.sh >> $ROOT/logs/cron.log 2>&1"
  echo "nohup $ROOT/.venv/bin/python $ROOT/scripts/serve_dist.py --port ${WEB_PORT} >> $ROOT/logs/serve.log 2>&1 &"
  echo
  log "（定时时间按服务器本地时区解释，脚本内部已经固定用 Asia/Shanghai）"
  exit 0
fi

# 以仓库所有者的身份跑：产物要写回 dist/ 和 .git，root 跑会留下 root 属主的文件。
if [ "$(id -u)" != "0" ]; then
  log "[失败] 装 systemd 定时任务需要 root：sudo bash deploy/install.sh"
  exit 1
fi
RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "$RUN_USER" = "root" ]; then
  log "[warn] 以 root 身份运行；建议用仓库属主执行 sudo，避免 dist/ 变成 root 属主"
fi

UNIT_DIR="/etc/systemd/system"
log "写入 $UNIT_DIR/${SERVICE_NAME}.service 和 .timer（执行用户 $RUN_USER）"

cat > "$UNIT_DIR/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=clash-daily 每日节点聚合（单机一条龙）
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${ROOT}
Environment=TZ=Asia/Shanghai
# 不想回写远端就把这一行改成 PUBLISH=0
Environment=PUBLISH=1
ExecStart=/usr/bin/env bash ${ROOT}/scripts/run_local.sh
# 一轮十几分钟，给足余量；卡死时由 systemd 收掉
TimeoutStartSec=4h
Nice=10
EOF

cat > "$UNIT_DIR/${SERVICE_NAME}.timer" <<EOF
[Unit]
Description=每天 ${RUN_AT} 跑一轮 clash-daily

[Timer]
OnCalendar=*-*-* ${RUN_AT}:00
# 服务器关机/休眠错过了就补跑一次
Persistent=true
Unit=${SERVICE_NAME}.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

log "已启用。查看下一次触发：systemctl list-timers ${SERVICE_NAME}.timer"
log "手动跑一轮：sudo systemctl start ${SERVICE_NAME}.service"
log "看日志：    journalctl -u ${SERVICE_NAME}.service -n 200 --no-pager"

# ---------- 4. 订阅站点 ----------
if [ "$NO_WEB" = "1" ]; then
  log "--no-web：跳过订阅 HTTP 站点"
  exit 0
fi

log "写入 $UNIT_DIR/${WEB_SERVICE_NAME}.service（端口 ${WEB_PORT}）"
cat > "$UNIT_DIR/${WEB_SERVICE_NAME}.service" <<EOF
[Unit]
Description=clash-daily 订阅站点（dist/ 直挂 HTTP，端口 ${WEB_PORT}）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${ROOT}
Environment=TZ=Asia/Shanghai
ExecStart=${ROOT}/.venv/bin/python ${ROOT}/scripts/serve_dist.py --bind 0.0.0.0 --port ${WEB_PORT} --dist ${ROOT}/dist
# 产物每天更新，进程本身只是读文件；崩了自动拉起即可
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${WEB_SERVICE_NAME}.service"

log "订阅站点已启动：http://<服务器IP>:${WEB_PORT}/"
log "  Clash：  http://<服务器IP>:${WEB_PORT}/clash.yaml"
log "  Base64： http://<服务器IP>:${WEB_PORT}/clash.base64.txt"
log "看日志：  journalctl -u ${WEB_SERVICE_NAME}.service -n 200 --no-pager"
log "放行端口：ufw allow ${WEB_PORT}/tcp  或  firewall-cmd --add-port=${WEB_PORT}/tcp --permanent && firewall-cmd --reload"
