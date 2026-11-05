#!/usr/bin/env bash
# RouterCat 启动脚本
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "未找到 python3，请先安装：sudo apt install -y python3" >&2
  exit 1
fi
exec "$PY" -u server.py "$@"
