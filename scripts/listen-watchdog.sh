#!/usr/bin/env bash
# listen 看门狗：由 cron 每 2 分钟调用一次。
# listen 进程不在就用 scripts/run_listen.sh 拉起。
# cron 托管不受 worker 会话回收影响；拉起后的 listen 是 setsid 脱离的
# 守护进程，不随本次 cron 执行结束被回收。
set -u

BRIDGE="/home/hatch/workspace/muse-feishu-bridge"
STATE_DIR="/home/hatch/hooks/state/feishu-inbox"
COOLDOWN=300          # 同一分钟内只尝试拉起一次，避免并发 cron 重复拉起

mkdir -p "$STATE_DIR"
lock="$STATE_DIR/listen-watchdog.lock"
# 防并发：同一时间只跑一个看门狗实例
if ! ( set -C; : > "$lock" ) 2>/dev/null; then
  exit 0
fi
trap 'rm -f "$lock"' EXIT

if pgrep -f "[m]use_feishu_bridge[.]cli listen" >/dev/null 2>&1; then
  rm -f "$STATE_DIR/listen_down_at"
  exit 0
fi

now=$(date +%s)
last=$(cat "$STATE_DIR/listen_down_at" 2>/dev/null || echo 0)
case "$last" in ''|*[!0-9]*) last=0 ;; esac
if [ $(( now - last )) -lt "$COOLDOWN" ]; then
  exit 0
fi
echo "$now" > "$STATE_DIR/listen_down_at"

cd "$BRIDGE"
setsid nohup scripts/run_listen.sh >/dev/null 2>&1 < /dev/null &
sleep 10
if pgrep -f "[m]use_feishu_bridge[.]cli listen" >/dev/null 2>&1; then
  echo "$(date -u +%FT%TZ) listen-watchdog: listen 不在，已拉起" >> "$BRIDGE/data/listen.log"
else
  echo "$(date -u +%FT%TZ) listen-watchdog: 拉起失败，见 data/listen.log" >> "$BRIDGE/data/listen.log"
fi
