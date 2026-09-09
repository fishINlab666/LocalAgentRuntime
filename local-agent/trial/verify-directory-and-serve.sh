#!/bin/sh
# Run from the terminal that already holds the model credentials.
set -u
task_directory=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 2
cd "$task_directory" || exit 2

python3 -m local_agent evaluate-directory --log-dir trial/results
evaluation_status=$?
if [ "$evaluation_status" -ne 3 ]; then
    printf '%s\n' '真实目录验收未全部通过，页面未启动。请查看上方结果及报告路径，交回当前任务核对。'
    exit "$evaluation_status"
fi

printf '%s\n' '12 次自动检查通过，仍待逐项语义复核。正在启动目录问答页面，请保持终端开启。'
exec python3 -m local_agent serve --workspace examples/discovery-workspace --port 8765 --open
