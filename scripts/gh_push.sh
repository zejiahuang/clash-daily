#!/usr/bin/env bash
# 把这一轮的产物复制一份到 GitHub（main 分支）。
#
# 用法：bash scripts/gh_push.sh
#
# 为什么要单独一个脚本、而不是让 GitHub 自己来拿：GitHub 那边的工作流在测速
# 结束时就退出了，之后的收尾（国家判定、去重、出订阅）都在 CNB 上做，
# 所以「把结果复制回去」只能是 CNB 主动推。
#
# ⚠️ 必须在 `ci_commit.sh` **之后**跑：那时候 dist/ 已经提交、工作区干净，
#    推上去的才是完整的一轮。
#
# ⚠️ 没有 GITHUB_TOKEN 时**不算失败**（exit 0）：主产物已经在 CNB 上了，
#    GitHub 只是副本。硬失败会让整轮变成红的，而其实什么都没少。
set -euo pipefail

REPO="${GITHUB_REPO:-zejiahuang/clash-daily}"
TOKEN="${GITHUB_TOKEN:-}"
BRANCH="${GH_BRANCH:-main}"

if [ -z "$TOKEN" ]; then
  echo "没有 GITHUB_TOKEN ⇒ 跳过「复制一份到 GitHub」"
  exit 0
fi

AUTH="$(printf 'x-access-token:%s' "$TOKEN" | base64 | tr -d '\n')"
gh() { git -c "http.extraheader=Authorization: Basic ${AUTH}" "$@"; }
GH_URL="https://github.com/${REPO}.git"

if [ -n "$(git status --porcelain -- dist | head -1)" ]; then
  echo "⚠️ dist/ 还有未提交的改动 —— 先跑 ci_commit.sh 再复制" >&2
  exit 1
fi

echo "复制到 github.com/${REPO} 的 ${BRANCH} 分支"
if gh push "$GH_URL" "HEAD:refs/heads/${BRANCH}"; then
  echo "已复制"
  exit 0
fi

# 被拒只可能是「GitHub 那边多了一个我们这儿没有的提交」（例如有人在网页上
# 改过 README）。merge 一下再推 —— 不用 rebase：rebase 会改写本地历史，
# 之后 ci_commit 往 CNB 推就需要 force。
echo "推送被拒，先合并 GitHub 上的改动再试"
gh fetch "$GH_URL" "$BRANCH"
if ! git merge --no-edit -m "chore: 合并 GitHub 上的改动" FETCH_HEAD; then
  git merge --abort || true
  echo "合并冲突 —— 手工处理后再跑一次；这一轮的主产物已经在 CNB 上，不影响使用" >&2
  exit 1
fi
if gh push "$GH_URL" "HEAD:refs/heads/${BRANCH}"; then
  echo "已复制（合并后）"
  exit 0
fi
echo "仍然推不上去 —— 检查 GITHUB_TOKEN 是否有 contents:write" >&2
exit 1
