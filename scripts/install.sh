#!/usr/bin/env bash
# 一键安装：创建虚拟环境并安装依赖
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "安装完成。用 .venv/bin/python -m muse_feishu_bridge.cli --help 查看命令，"
echo "或执行: export PYTHONPATH=src && .venv/bin/python -m muse_feishu_bridge.cli doctor"
