@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 正在创建 Python 虚拟环境...
  py -3 -m venv .venv
  if errorlevel 1 goto :fail
)
echo 正在安装依赖...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo 正在安装 Playwright Chromium...
.\.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
  echo Chromium 安装失败。请检查网络后重新运行本脚本。
  goto :fail
)
echo 启动知乎内容搜集器...
.\.venv\Scripts\python.exe -m zhihu_collector
exit /b 0

:fail
echo 启动失败。
pause
exit /b 1
