@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0
title literature_flow uninstaller
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ============================================================
echo   literature_flow  -  uninstaller
echo ============================================================
echo.
echo   This scans everything literature_flow downloaded or cached,
echo   lists each item with its size, and asks you to confirm
echo   before deleting anything.
echo.
echo   Run with --list to scan only, without deleting anything.
echo.

REM --- Pick an interpreter OUTSIDE webapp\venv if possible -----------
REM The venv is itself one of the things we may delete, and Windows
REM refuses to delete a folder that holds the running python.exe.
set "PYEXE="
if not defined PYEXE for /f "delims=" %%P in ('py -3 -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "PYEXE=%%P"
for %%V in (313 312 311 310) do (
    if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE if exist "venv\Scripts\python.exe" set "PYEXE=venv\Scripts\python.exe"

if not defined PYEXE (
    echo   ERROR: no Python interpreter found. Install Python 3.10+ first.
    echo.
    pause
    exit /b 1
)

"%PYEXE%" uninstall.py %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
