#!/usr/bin/env bash
# 在亚太机器（香港 / 日本 / 新加坡…）上安装 GitHub self-hosted runner。
#
# 用法：
#   RUNNER_TOKEN=<注册令牌> bash scripts/install-runner.sh
#
# 可选环境变量：
#   REPO     默认 zejiahuang/clash-daily
#   LABELS   默认 asia（必须与仓库变量 RUNNER_LABEL 的值一致）
#   NAME     默认 <主机名>-asia
#   DIR      默认 $HOME/actions-runner
#
# 注册令牌从哪来（有效期 1 小时，用完即失效）：
#   curl -sX POST -H "Authorization: Bearer <PAT>" \
#     https://api.github.com/repos/zejiahuang/clash-daily/actions/runners/registration-token
#   或在仓库页面 Settings → Actions → Runners → New self-hosted runner 里取 --token 的值。

set -euo pipefail

REPO="${REPO:-zejiahuang/clash-daily}"
LABELS="${LABELS:-asia}"
NAME="${NAME:-$(hostname)-asia}"
DIR="${DIR:-$HOME/actions-runner}"
TOKEN="${RUNNER_TOKEN:-}"

if [ -z "$TOKEN" ]; then
  echo "缺少 RUNNER_TOKEN。先取注册令牌：" >&2
  echo "  curl -sX POST -H 'Authorization: Bearer <PAT>' \\" >&2
  echo "    https://api.github.com/repos/${REPO}/actions/runners/registration-token" >&2
  exit 1
fi

# sudo 前缀：root 直接跑，普通用户用 sudo
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

case "$(uname -m)" in
  x86_64|amd64)  ARCH="x64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "不支持的架构：$(uname -m)" >&2; exit 1 ;;
esac

echo "==> 目标仓库 ${REPO}"
echo "==> 架构 linux-${ARCH}，标签 ${LABELS}，名称 ${NAME}"

echo "==> 安装基础依赖"
missing=()
for bin in curl tar git; do
  command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
command -v python3 >/dev/null 2>&1 || missing+=("python3" "python3-venv" "python3-pip")
if [ ${#missing[@]} -gt 0 ]; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq "${missing[@]}"
fi

echo "==> 查询 runner 最新版本"
VER=$(curl -sSL --max-time 25 "https://api.github.com/repos/actions/runner/releases/latest" \
      | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1)
if [ -z "$VER" ]; then
  echo "拿不到 runner 版本号（GitHub API 不可达？）" >&2
  exit 1
fi
echo "    版本 v${VER}"

PKG="actions-runner-linux-${ARCH}-${VER}.tar.gz"
URL="https://github.com/actions/runner/releases/download/v${VER}/${PKG}"

mkdir -p "$DIR"
cd "$DIR"

if [ -f ".runner" ]; then
  echo "==> ${DIR} 已配置过 runner，跳过下载与注册"
else
  echo "==> 下载 ${PKG}"
  curl -fL --retry 3 --max-time 600 -o "$PKG" "$URL"
  SIZE=$(stat -c%s "$PKG" 2>/dev/null || stat -f%z "$PKG")
  if [ "$SIZE" -lt 40000000 ]; then
    echo "下载文件只有 ${SIZE} 字节，明显不对（正常约 100 MB）" >&2
    exit 1
  fi
  tar xzf "$PKG"

  echo "==> 注册 runner"
  ./config.sh \
    --url "https://github.com/${REPO}" \
    --token "$TOKEN" \
    --name "$NAME" \
    --labels "$LABELS" \
    --work "_work" \
    --unattended \
    --replace
fi

echo "==> 安装为 systemd 服务（开机自启）"
if [ ! -f ".service" ]; then
  $SUDO ./svc.sh install "$(id -un)"
fi
$SUDO ./svc.sh start

echo
echo "==> 完成。检查状态："
$SUDO ./svc.sh status || true
echo
echo "下一步："
echo "  1. 仓库 Settings → Actions → Runners，确认这台机器状态是 Idle"
echo "     并核对标签里有 ${LABELS}"
echo "  2. 仓库 Settings → Secrets and variables → Actions → Variables"
echo "     新建 RUNNER_LABEL = ${LABELS}"
echo "  3. Actions → 每日节点聚合 → Run workflow，看日志里"
echo "     '记录测速机位置' 那一步输出的国家"
