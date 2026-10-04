#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
binding_file="${1:-runs/feishu-binding.json}"
state_directory="${2:-$HOME/.local/share/local-agent}"
if [[ ! -x .venv/bin/python || ! -f "$binding_file" ]]; then
  echo '请先安装 requirements-feishu.txt，并运行 .venv/bin/python trial/prepare-feishu.py。'
  exit 2
fi
if [[ -z "${FEISHU_APP_SECRET:-}" ]]; then
  read -r -s -p '请输入飞书 App Secret（不显示、不保存）：' FEISHU_APP_SECRET
  echo
fi
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  read -r -s -p '请输入 DeepSeek API Key（不显示、不保存）：' DEEPSEEK_API_KEY
  echo
fi
export FEISHU_APP_SECRET DEEPSEEK_API_KEY
session_identifier="$(.venv/bin/python -c 'import sys; from local_agent.channels.config import load_config; print(load_config(sys.argv[1])["session_id"])' "$binding_file")"
exec .venv/bin/python -m local_agent serve --session "$session_identifier" \
  --state-dir "$state_directory" --feishu-config "$binding_file" --port 8780 --open
