@echo off
setlocal
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo [Loi] Chua tim thay virtual environment tai backend\.venv
    echo Hay chay: python -m venv backend\.venv  roi  backend\.venv\Scripts\pip install -r backend\requirements.txt
    pause
    exit /b 1
)

echo Dang khoi dong server tai http://localhost:8000 ...
start "" http://localhost:8000
".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000

pause
