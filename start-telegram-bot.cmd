@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Create the environment first: python -m venv .venv
    echo Then install dependencies: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0telegram_bot.py" %*
pause
