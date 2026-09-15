#!/bin/bash
# One local entry for the fixed synthetic acceptance; credentials stay in this process environment.
set +x
set -eu
cd -- "$(dirname -- "$0")/.."
if [ "$#" -gt 1 ]; then
  echo '用法：verify-extensions.command [上一次真实验收 report.json]'
  exit 2
fi
if [ ! -x .venv/bin/python ]; then
  echo '未找到扩展 Python 环境，请先按照 README 安装 requirements-extensions.txt。'
  exit 2
fi
if [ -z "${DEEPSEEK_API_KEY:-${AGENT_API_KEY:-}}" ]; then
  read -r -s -p '输入 DeepSeek API Key（输入时不显示）：' DEEPSEEK_API_KEY
  echo
  export DEEPSEEK_API_KEY
fi
echo '开始固定真实验收：只发送新建的合成资料，最多 6 个 Run / 60 次模型请求。'
if [ "$#" -eq 1 ]; then
  exec .venv/bin/python scripts/evaluate_extensions.py --real --prior-report "$1"
fi
exec .venv/bin/python scripts/evaluate_extensions.py --real
