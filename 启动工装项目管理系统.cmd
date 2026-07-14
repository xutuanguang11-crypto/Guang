@echo off
setlocal
set "PYTHON=C:\Users\GuangTou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
cd /d "%~dp0"
"%PYTHON%" server.py 8902 --open
if errorlevel 1 (
  echo.
  echo 系统启动失败，请保留本窗口并截图错误内容。
  pause
)
endlocal
