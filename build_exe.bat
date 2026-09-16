@echo off
REM Build the portable (green) distribution of literature_flow.
REM Usage: run from project root. Requires _build\venv (see README section "Build exe").
cd /d %~dp0

if not exist _build\venv\Scripts\python.exe (
    echo [build] Build venv missing. Create it first:
    echo [build]   python -m venv _build\venv
    echo [build]   _build\venv\Scripts\pip install pyinstaller -r webapp\requirements.txt
    exit /b 1
)

echo [build] Running PyInstaller (a few minutes)...
_build\venv\Scripts\pyinstaller build_exe.spec --noconfirm
if errorlevel 1 (
    echo [build] ERROR: PyInstaller failed.
    exit /b 1
)

REM writable dirs inside the package
if not exist dist\literature_flow\cache mkdir dist\literature_flow\cache
if not exist dist\literature_flow\reports mkdir dist\literature_flow\reports
if not exist dist\literature_flow\_internal\config mkdir dist\literature_flow\_internal\config

REM bundle chromium kernel if the local config points to one (does NOT fail the build)
set "KB="
for /f "usebackq delims=" %%p in (`_build\venv\Scripts\python.exe -c "import json,os;p=os.path.join('webapp','config','paths.json');print(json.load(open(p,encoding='utf-8')).get('playwright_browsers_dir','') if os.path.exists(p) else '')"`) do set "KB=%%p"
if defined KB if exist "%KB%" (
    echo [build] Bundling chromium kernel from "%KB%" ...
    robocopy "%KB%" dist\literature_flow\webchat_browsers /E /NFL /NDL /NJH /NJS >nul
)
if not exist dist\literature_flow\webchat_browsers echo [build] NOTE: no chromium kernel bundled; users must run "playwright install chromium" for webchat features.

echo [build] Zipping...
powershell -NoProfile -Command "Compress-Archive -Path dist\literature_flow\* -DestinationPath dist\literature_flow-win64.zip -Force"
echo [build] Done: dist\literature_flow-win64.zip
