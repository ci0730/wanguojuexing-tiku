@echo off
cd /d "%~dp0"
echo [CMD调试] 启动万国觉醒题库...
echo 关闭本窗口不会立即退出软件窗口；请先关软件窗口。
echo.
python desktop.py --console
if errorlevel 1 (
  echo.
  echo 启动失败，请检查是否已安装依赖: pip install -r requirements.txt
  pause
)
