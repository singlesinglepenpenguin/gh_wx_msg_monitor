@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PATH=%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"

rem Windows equivalent of setup.command:
rem create .venv and install requirements.txt (openpyxl / pycryptodome / zstandard).
rem Daily use: start.bat (it calls this automatically when .venv is missing).

set "QUIET=0"
if /I "%~1"=="--quiet" set "QUIET=1"

rem Customer distribution contains Python, Tk and all native dependencies.
rem Setup validates the shipped executable, then opens the GUI connection wizard.
if exist "GroupMessageMonitor.exe" goto packaged

if defined GROUP_MSG_MONITOR_PYTHON (
  set "PYTHON_BIN=%GROUP_MSG_MONITOR_PYTHON%"
) else (
  set "PYTHON_BIN=python"
)

"%PYTHON_BIN%" -c "import sys; assert sys.version_info >= (3, 10), 'need Python 3.10+'; import tkinter" 1>nul 2>nul
if errorlevel 1 (
  echo [错误] 请先安装带 Tkinter 的 Python 3.10 或更新版本，再运行 setup.bat / start.bat。
  echo 官网: https://www.python.org/downloads/  安装时勾选 "tcl/tk" 与 "Add python.exe to PATH"。
  if "%QUIET%"=="0" pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  echo [1/3] 已有 .venv，跳过创建，直接更新依赖...
) else (
  echo [1/3] 创建虚拟环境 .venv ...
  "%PYTHON_BIN%" -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。
    if "%QUIET%"=="0" pause
    exit /b 1
  )
)

echo [2/3] 安装运行依赖 ^(openpyxl / pycryptodome / zstandard^) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo 默认 PyPI 失败，改用清华镜像重试...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    if "%QUIET%"=="0" pause
    exit /b 1
  )
)

echo [3/3] 检查运行时依赖...
set "PYTHONPATH=%CD%\src"
".venv\Scripts\python.exe" -m group_msg_monitor --check-runtime
if errorlevel 1 (
  echo [错误] 运行时检查失败。
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo.
echo 环境安装完成。日常请双击 start.bat 启动。
if "%QUIET%"=="0" ".venv\Scripts\python.exe" -m group_msg_monitor --setup
endlocal
exit /b 0

:packaged
if not exist "_internal\vendor\wechat_native\scripts\windows_wechat_get_key.ps1" (
  echo [PACKAGE_DEPENDENCY] 请完整解压交付包后再运行 setup.bat，不要只复制 exe 或 bat。
  if "%QUIET%"=="0" pause
  exit /b 1
)
echo 正在检查工具文件，请稍候...
"%~dp0GroupMessageMonitor.exe" --check-runtime 1>nul 2>nul
if errorlevel 1 (
  echo [PACKAGE_DEPENDENCY] 完整性检查失败，请查看 data\local_data\error_message.jsonl。
  if "%QUIET%"=="0" pause
  exit /b 1
)
if "%QUIET%"=="0" "%~dp0GroupMessageMonitor.exe" --setup
endlocal
exit /b 0
