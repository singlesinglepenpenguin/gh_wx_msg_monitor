#!/bin/zsh
# Install source dependencies once; never reads or initializes WeChat.
set -eu
cd -- "${0:A:h}"
python_bin="${GROUP_MSG_MONITOR_PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10 或更新版本"; import tkinter' || {
  print '请先安装带 Tkinter 的 Python 3.10 或更新版本，再运行 setup.command。'
  exit 1
}
"$python_bin" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PYTHONPATH="$PWD/src" .venv/bin/python -m group_msg_monitor --check-runtime
print '环境安装完成。请双击 start.command，选择自己的通讯录和更新基准。'
