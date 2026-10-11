#!/usr/bin/env bash
# 启动飞书长连接监听（daemon 化）。Muse 的保活 cron 用它来拉起。
cd "$(dirname "$0")/.."
export PYTHONPATH=src
exec .venv/bin/python -m muse_feishu_bridge.cli listen >> data/listen.log 2>&1
