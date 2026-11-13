@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo [提示] 尚未发现 Python 虚拟环境，正在转入首次安装向导...
    call "安装并启动.bat"
    exit /b %errorlevel%
)

echo 正在启动知乎内容搜集器...
start "" ".\.venv\Scripts\pythonw.exe" -m zhihu_collector
if errorlevel 1 (
    echo [提示] 尝试使用标准 Python 启动...
    .\.venv\Scripts\python.exe -m zhihu_collector
)
exit /b 0
