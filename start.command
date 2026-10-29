#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print '首次使用请先运行同目录的 setup.command 安装环境，再打开 start.command。'
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m group_msg_monitor
