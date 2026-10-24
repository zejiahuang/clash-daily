#!/usr/bin/env bash
# 把 dist/ 的改动提交回仓库 —— 三个入口（手动 / 定时 / 自检）共用这一份。
#
# 用法：bash scripts/ci_commit.sh "提交说明"
#
# ⚠️ 防自触发：本机器人写的产物提交会**再触发一次 push 构建**。第二次构建里
#    「上一提交就是产物提交」，于是在这里直接退出 —— 没有新提交，也就不会再
#    触发第三次。**不要删这个判断**，删了就是无限循环。
#
# ⚠️ 判据必须**同时**满足两条：① 作者是 cnb-bot；② 改动全部落在 dist/ 里。
#    只看作者会误伤，而且是**静默**误伤：谁用 cnb-bot 的邮箱提交代码（本地手工也
#    算），这条判断就把产物提交跳过，而阶段照样显示成功、日志只多一行「跳过」。
#    2026-10-24 就这么踩了一次 —— 推上去之后 `dist/probe.json` 一直停在旧时间戳，
#    从外部看是「构建成功但没有新产物」，查了半小时才想到是作者邮箱。
set -euo pipefail

MSG="${1:-}"
if [ -z "$MSG" ]; then
  echo "用法：bash scripts/ci_commit.sh \"提交说明\"" >&2
  exit 2
fi

BOT_MAIL="cnb-bot@cnb.cool"
if [ "$(git log -1 --format=%ae)" = "$BOT_MAIL" ]; then
  # 非 dist/ 的改动行（空行剔掉）；为空 ⇒ 上一提交只动了产物
  changed="$(git show --pretty=format: --name-only HEAD | grep -v '^dist/' | grep -v '^$' || true)"
  if [ -z "$changed" ]; then
    echo "上一提交是产物提交（cnb-bot 且只改 dist/），跳过（防自触发）"
    exit 0
  fi
  echo "⚠️ 上一提交作者是 cnb-bot，但改了非 dist/ 的文件 —— 这不是产物提交，继续"
  echo "$changed" | sed 's/^/     /'
fi

git config user.name  "cnb-bot"
git config user.email "cnb-bot@cnb.cool"

if [ ! -d dist ]; then
  echo "没有 dist/ 目录，跳过提交"
  exit 0
fi

git add dist
if git diff --cached --quiet; then
  echo "dist/ 没有变化，跳过提交"
  exit 0
fi
git commit -m "$MSG"

# ⚠️ 推送前必须先 rebase。一轮构建十几分钟，这期间**远端会动**：人手工推了代码，
#    或者另一个流水线刚提交了产物。在旧基线上直接 push 会被拒（`! [rejected]
#    (fetch first)`），而这一步失败 = 整轮产物丢失，外部只看到「阶段变红」，
#    产物文件停在上一轮的时间戳 —— 和「构建没跑」长得一模一样。
#    2026-10-24 本地手工推的时候连着踩了三次，每次都得手动 fetch + rebase。
#
# 注意 rebase 的是**产物提交**（只动 dist/），落在新代码之上是安全的；
# 真冲突（两个流水线同时写 dist/）就 abort 并失败退出，不硬来。
sync_and_push() {
  local url="$1"
  local attempt
  for attempt in 1 2 3; do
    if git push "$url"; then
      return 0
    fi
    echo "push 被拒（第 ${attempt} 次），fetch + rebase 到远端后重试"
    if ! git fetch "$url" HEAD; then
      echo "  fetch 失败，放弃"
      return 1
    fi
    if ! git rebase FETCH_HEAD; then
      git rebase --abort || true
      echo "  rebase 冲突（多半是两个流水线同时写 dist/），放弃"
      return 1
    fi
  done
  return 1
}

# 检出用的凭据不一定带写权限。先试默认，失败再显式带 CNB_TOKEN。
# ⚠️ 失败时把 CNB_* 环境变量打出来 —— 变量名猜错的话下次还是错的，
#    打出来才知道真名。
if sync_and_push origin; then
  exit 0
fi

echo "默认 push 失败，改用 CNB_TOKEN 重试"
if [ -z "${CNB_REPO_URL_HTTPS:-}" ] && [ -z "${CNB_REPO_SLUG:-}" ]; then
  echo "===== 拼不出带凭据的地址，打印环境以便定位 ====="
  env | grep -i '^CNB_' | sed 's/\(TOKEN[A-Z_]*=\).*/\1***/' | sort
  exit 1
fi
if [ -z "${CNB_TOKEN:-}" ]; then
  echo "===== CNB_TOKEN 未注入，打印环境以便定位 ====="
  env | grep -i '^CNB_' | sort
  exit 1
fi

base="${CNB_REPO_URL_HTTPS:-https://cnb.cool/${CNB_REPO_SLUG}.git}"
if sync_and_push "https://cnb:${CNB_TOKEN}@${base#https://}"; then
  exit 0
fi
echo "带凭据重试也失败 —— 产物没提交上去"
exit 1
