#!/usr/bin/env bash
# 启动飞书长连接监听（daemon 化），带进程级自重启。
# listen 内部已有断线重连（指数退避）；这里只兜底"python 进程整个退出"
# 的情况：等 10 秒重拉。连续 5 次 30 秒内快速退出则判定为配置类问题，
# 等 5 分钟再试，避免打爆日志。机器级保活由外部看门狗承担
# （scripts/listen-watchdog.sh，挂在平台 cron 上每 2 分钟跑一次）。
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=src

crashes=0
while true; do
  start=$(date +%s)
  .venv/bin/python -m muse_feishu_bridge.cli listen >> data/listen.log 2>&1
  rc=$?
  echo "$(date -u +%FT%TZ) run_listen: listen 进程退出（rc=$rc），准备重拉" >> data/listen.log
  if [ $(( $(date +%s) - start )) -lt 30 ]; then
    crashes=$(( crashes + 1 ))
  else
    crashes=0
  fi
  if [ "$crashes" -ge 5 ]; then
    echo "$(date -u +%FT%TZ) run_listen: 连续 5 次快速退出，等 5 分钟再试" >> data/listen.log
    sleep 300
    crashes=0
  else
    sleep 10
  fi
done
