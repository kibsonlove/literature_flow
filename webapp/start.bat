@echo off
cd /d %~dp0
REM If port 8000 is already in use (e.g. a previous background instance), free it first to avoid double-launch conflict
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 >nul
REM Open the web page in the default browser 3s later (server should be ready by then)
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"
REM Prefer the project venv, fall back to system python
set "PYEXE=venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
