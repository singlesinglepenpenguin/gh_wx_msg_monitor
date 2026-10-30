@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PATH=%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"

rem Packaged distribution: no Python installation is required.
if exist "GroupMessageMonitor.exe" goto packaged

rem Windows equivalent of start.command.
rem First launch without .venv automatically runs setup.bat once.

if not exist ".venv\Scripts\python.exe" (
  echo 首次使用：正在安装本地环境（只需一次，需联网）...
  call "%~dp0setup.bat" --quiet
  if errorlevel 1 (
    echo [错误] 环境安装失败，无法启动。
    pause
    exit /b 1
  )
)

set "PYTHONDONTWRITEBYTECODE=1"
if defined PYTHONPATH (
  set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%CD%\src"
)

".venv\Scripts\python.exe" -m group_msg_monitor
goto result

:packaged
"%~dp0GroupMessageMonitor.exe"

:result
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo [错误] 程序退出码 %EXIT_CODE%
  pause
)
endlocal & exit /b %EXIT_CODE%
