#!/usr/bin/env bash
# CNB → GitHub 的交接：把候选推过去，等测速结果回来。
#
# 用法：bash scripts/handoff_github.sh [dist/candidates.json]
#
# 流程：
#   1. 把 dist/candidates.json 提成一个本地提交（作者**故意不是** cnb-bot，
#      否则最后那次 ci_commit 会被「防自触发」守卫当成产物提交跳过）；
#   2. 强推到 GitHub 的 ci-handoff 分支 —— 这一步本身就是 GitHub 侧的触发器
#      （.github/workflows/speedtest.yml 监听这个分支上的 dist/candidates.json），
#      不需要任何 API、不需要 api.github.com 可达；
#   3. 轮询 raw.githubusercontent.com 上的 dist/speed.json，直到它的 run_id
#      与本次候选的 run_id 一致；写成 dist/speed.json 交给下一段。
#
# ⚠️ 分支名**不能带斜杠**：raw 地址是 /{owner}/{repo}/{branch}/{path}，
#    分支名带斜杠时靠「最长前缀匹配」猜，猜错就是 404 —— 而这边只表现为
#    「一直等不到结果」，看起来像 GitHub 没跑。
#
# ⚠️ 轮询**必须比对 run_id**，不能只看文件存在：speed.json 是提交进仓库的文件，
#    上一轮那份会一直在。只判存在的话，GitHub 没跑起来时这里会安静地拿到旧结果。
set -euo pipefail

CANDIDATES="${1:-dist/candidates.json}"
SPEED_OUT="${SPEED_OUT:-dist/speed.json}"
BRANCH="${HANDOFF_BRANCH:-ci-handoff}"
REPO="${GITHUB_REPO:-zejiahuang/clash-daily}"
TOKEN="${GITHUB_TOKEN:-}"
TIMEOUT_MIN="${HANDOFF_TIMEOUT_MIN:-45}"
INTERVAL="${HANDOFF_INTERVAL:-15}"
# 秒数总预算。默认由 TIMEOUT_MIN 算出来；单独留一个开关是给测试用的
# —— 测试里等 45 分钟没有意义，等 3 秒就能验证「拿不到就不接受」。
TIMEOUT_S="${HANDOFF_TIMEOUT_S:-$((TIMEOUT_MIN * 60))}"
# 这两个地址可以覆盖，测试时指向本地裸仓库 + 本地 HTTP 服务（见 tests/test_handoff.py）
GH_URL="${GITHUB_URL:-https://github.com/${REPO}.git}"
RAW_BASE="${HANDOFF_RAW_BASE:-https://raw.githubusercontent.com/${REPO}/${BRANCH}}"

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
echo "本轮 run_id=$RUN_ID"

if [ -z "$TOKEN" ]; then
  echo "===== 没有 GITHUB_TOKEN，交接无法进行 =====" >&2
  echo "需要在 CNB 仓库的「环境变量 / 密钥」里加一个 GITHUB_TOKEN（GitHub PAT，" >&2
  echo "至少带 contents:write），它只用来把候选推过去、把产物复制回去。" >&2
  exit 1
fi

# 令牌不进 URL：出错时 git 会把整个 URL 打出来，包括凭据。
# 用 http.extraheader 传 Basic 认证，git 不会回显 header。
AUTH="$(printf 'x-access-token:%s' "$TOKEN" | base64 | tr -d '\n')"
gh() { git -c "http.extraheader=Authorization: Basic ${AUTH}" "$@"; }

# 1. 本地提交（作者不是 cnb-bot）------------------------------------------
git add "$CANDIDATES"
# --allow-empty：内容万一没变也必须有一个新提交，否则推上去没有新 commit，
# GitHub 那边不会触发（它是按 push 事件里的 diff 过滤 dist/candidates.json 的）
git -c user.name="clash-daily-ci" -c user.email="ci@local" \
  commit --allow-empty -m "chore(handoff): 候选 ${RUN_ID}"

# 2. 强推到 ci-handoff ----------------------------------------------------
echo "推送到 github.com/${REPO} 的 ${BRANCH} 分支"
if ! gh push --force "$GH_URL" "HEAD:refs/heads/${BRANCH}"; then
  echo "推送失败。常见原因：① GITHUB_TOKEN 没有 contents:write；② CNB runner 到 github.com 不通" >&2
  exit 1
fi
echo "已推送。GitHub 工作流应在十几秒内启动："
echo "  https://github.com/${REPO}/actions"

# 3. 轮询回传 -------------------------------------------------------------
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
    echo "  1) https://github.com/${REPO}/actions 里 speedtest 工作流有没有跑、红在哪一步" >&2
    echo "  2) 分支 ${BRANCH} 上有没有 dist/candidates.json（没有 ⇒ 推送没成功）" >&2
    echo "  3) 工作流跑完了但没提交 ⇒ 看它日志里「没有 speed.json，不提交」那一行" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
