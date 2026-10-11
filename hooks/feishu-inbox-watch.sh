#!/usr/bin/env bash
# feishu-inbox-watch：飞书桥的事件触发器（polling script 模板）。
#
# 每 30 秒跑一次，做两件事：
#   1. 保活：listen 长连接进程不在时自动拉起；
#   2. 扫收件箱：有 status=new 的消息时唤醒一个 worker（Muse）来处理。
#
# 运行时契约（由你的 agent 调度环境提供）：
#   - 环境变量 HATCH_HOOK_RUNTIME：一个可 source 的 shell 片段，
#     提供 log / silent / wake 三个函数：
#       log   "文字"        -> 记录一行日志（继续执行）
#       silent "文字" '{}'  -> 静默结束（无新事件，本次不唤醒）
#       wake  "理由" '{"event":...}' -> 唤醒 worker，把 JSON 作为事件 payload
#     如果没有这个运行时（纯手工测试），脚本退化为只打印到 stdout。
#   - 环境变量 HATCH_HOOK_DRY_RUN=1 时不写任何状态文件。
#
# 可调参数（环境变量覆盖）：
#   BRIDGE_DIR  仓库根目录，默认 $HOME/workspace/muse-feishu-bridge
#   STATE_DIR   触发器状态目录，默认 $HOME/hooks/state/feishu-inbox
#   POLL_SECS   无用（保留给调度器参考）：轮询间隔建议 30 秒
#   STALE_SECS  wake 后 N 秒仍未处理则重试唤醒，默认 600（10 分钟）
#
# 无 secrets、无凭证，只读本地文件。config.yaml 里的密钥永不进仓库。
set -euo pipefail

BRIDGE="${BRIDGE_DIR:-$HOME/workspace/muse-feishu-bridge}"
INBOX="$BRIDGE/data/inbox.jsonl"
STATE_DIR="${STATE_DIR_OVERRIDE:-$HOME/hooks/state/feishu-inbox}"
SEEN="$STATE_DIR/seen.json"          # message_id -> 首次 wake 的时间戳
STALE_SECS="${STALE_SECS:-600}"      # wake 后 10 分钟仍未处理则重试
DRY="${HATCH_HOOK_DRY_RUN:-0}"

# ---- 运行时函数：有 hook runtime 就用它的，没有就退化为 echo ----
if [ -n "${HATCH_HOOK_RUNTIME:-}" ] && [ -f "$HATCH_HOOK_RUNTIME" ]; then
  # shellcheck disable=SC1090
  source "$HATCH_HOOK_RUNTIME"
else
  log()    { echo "[log] $*"; }
  silent() { echo "[silent] $1"; }
  wake()   { echo "[wake] $1 :: $2"; }
fi

mkdir -p "$STATE_DIR"
[ -f "$SEEN" ] || echo '{}' > "$SEEN"

# ---- 1. 保活：listen 进程还在吗 ----
if ! pgrep -f "[m]use_feishu_bridge[.]cli listen" >/dev/null 2>&1; then
  log "listen 进程不在，尝试重启"
  (cd "$BRIDGE" && setsid nohup scripts/run_listen.sh >/dev/null 2>&1 < /dev/null &)
  sleep 8
  if pgrep -f "[m]use_feishu_bridge[.]cli listen" >/dev/null 2>&1; then
    log "listen 重启成功"
  else
    wake "飞书监听进程掉了且重启失败" '{"event":"listen_restart_failed"}'
    exit 0
  fi
fi

# ---- 2. 收件箱：找 status=new 的消息 ----
if [ ! -f "$INBOX" ]; then
  silent "收件箱还不存在" '{}'
  exit 0
fi

new_ids=$(jq -r 'select(.status=="new") | .message_id' "$INBOX" 2>/dev/null | sort -u)
if [ -z "$new_ids" ]; then
  silent "无新消息" '{}'
  exit 0
fi

now=$(date +%s)
to_wake=()
while IFS= read -r mid; do
  [ -n "$mid" ] || continue
  seen_at=$(jq -r --arg m "$mid" '.[$m] // 0' "$SEEN")
  if [ "$seen_at" = "0" ] || [ $(( now - seen_at )) -gt $STALE_SECS ]; then
    to_wake+=("$mid")
  fi
done <<< "$new_ids"

if [ "${#to_wake[@]}" -eq 0 ]; then
  silent "新消息都已唤起过处理" '{}'
  exit 0
fi

# ---- 立即 Get：扫到新消息先加"已收到"表情，不等 worker 冷启动 ----
# 只对收件箱中尚无 reaction_id 的记录操作；任何失败只 log，绝不阻断 wake。
if [ "$DRY" != "1" ] && [ "${#to_wake[@]}" -gt 0 ]; then
  no_react=()
  while IFS= read -r mid; do
    [ -n "$mid" ] || continue
    rid=$(jq -r --arg m "$mid" 'select(.message_id==$m) | .reaction_id // ""' "$INBOX" 2>/dev/null | head -n1 || true)
    [ -z "$rid" ] && no_react+=("$mid")
  done <<< "$(printf '%s\n' "${to_wake[@]}")"
  if [ "${#no_react[@]}" -gt 0 ]; then
    react_tsv=$(timeout 25 env PYTHONPATH="$BRIDGE/src" "$BRIDGE/.venv/bin/python" \
      -m muse_feishu_bridge.cli react-batch --emoji Get "${no_react[@]}" 2>/dev/null || true)
    while IFS=$'\t' read -r rmid rrid; do
      [ -n "$rmid" ] && [ -n "$rrid" ] || continue
      PYTHONPATH="$BRIDGE/src" "$BRIDGE/.venv/bin/python" -c \
        'import sys; from muse_feishu_bridge import inbox; inbox.set_reaction_id(sys.argv[1], sys.argv[2], sys.argv[3])' \
        "$INBOX" "$rmid" "$rrid" 2>/dev/null || true
    done <<< "$react_tsv"
    log "已为 ${#no_react[@]} 条新消息尝试加 Get 表情" '{}'
  fi
fi

# 记录已唤起，避免重复 wake（dry run 不写 state）
if [ "$DRY" != "1" ]; then
  tmp=$(mktemp)
  jq --argjson now "$now" \
    '. + ($ARGS.positional | map({(.): $now}) | add)' \
    "$SEEN" --args "${to_wake[@]}" > "$tmp" && mv "$tmp" "$SEEN"
fi

payload=$(printf '%s\n' "${to_wake[@]}" | jq -R . | jq -s '{event:"new_messages", message_ids: .}')
wake "飞书收到 ${#to_wake[@]} 条新消息" "$payload"
exit 0
