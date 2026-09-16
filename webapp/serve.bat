@echo off
REM Server-only launcher: no browser tab is opened (use an existing tab and refresh).
REM Used by automated restarts; start.bat remains the user-facing launcher.
cd /d %~dp0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 >nul
if not exist logs mkdir logs
REM Prefer the project venv, fall back to system python
set "PYEXE=venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" -m uvicorn app:app --host 127.0.0.1 --port 8000 >> logs\server.log 2>&1
