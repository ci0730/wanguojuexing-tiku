@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo 未找到 pythonw，请确认已安装 Python 并勾选 Add to PATH
  pause
  exit /b 1
)
start "" /B pythonw desktop.py
exit
