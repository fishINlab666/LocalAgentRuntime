#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo '缺少项目 Python 环境，请先按 README 安装。'
  exit 2
fi
.venv/bin/python -c 'from local_agent.channels.feishu import require_sdk_version; require_sdk_version()'
if [[ ! -f runs/feishu-binding.json ]]; then
  if [[ -z "${FEISHU_APP_SECRET:-}" ]]; then
    read -r -s -p '请输入飞书 App Secret（不显示、不保存）：' FEISHU_APP_SECRET
    echo
  fi
  export FEISHU_APP_SECRET
  .venv/bin/python trial/prepare-feishu.py --listen
fi
exec bash trial/start-feishu.command
