@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo ❌ 找不到 python，請先安裝 Python 3.10+ & pause & exit /b 1)
python -c "import requests, yaml, dotenv, openpyxl" 2>nul || (
    echo 首次執行：安裝相依套件...
    python -m pip install -r requirements.txt || (echo ❌ 套件安裝失敗 & pause & exit /b 1)
)
start "" pythonw -m projectsync %*
