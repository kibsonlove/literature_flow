@echo off
cd /d %~dp0
setlocal

echo ============================================================
echo   literature_flow  -  first-time setup
echo ============================================================
echo.
echo  This script will:
echo    1) find a Python interpreter on this computer
echo    2) create a local virtual environment in webapp\venv
echo    3) install the required Python packages
echo    4) detect your Zotero folders and save them into config
echo.
echo  It does NOT touch your Zotero library or your system Python.
echo.
pause

echo.
echo [1/4] Looking for Python...

if exist "venv\Scripts\python.exe" (
    echo       found an existing virtual environment, will reuse it
    set "PYEXE=venv\Scripts\python.exe"
    goto have_python
)

where python >nul 2>&1
if %errorlevel%==0 goto try_python
where py >nul 2>&1
if %errorlevel%==0 goto try_py
goto no_python

:try_python
REM Resolve to an absolute path and check the version: this project needs 3.10+,
REM an older interpreter would fail later during "pip install" with a cryptic error.
set "CAND="
for /f "delims=" %%P in ('python -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "CAND=%%P"
if not defined CAND goto try_py
"%CAND%" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto python_too_old
set "PYEXE=%CAND%"
echo       using the system Python:
"%PYEXE%" --version
goto have_python

:python_too_old
echo       the Python on PATH is too old for this project (needs 3.10+):
"%CAND%" --version
goto try_py

:try_py
set "CAND2="
where py >nul 2>&1
if not %errorlevel%==0 goto no_python
for /f "delims=" %%P in ('py -3 -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "CAND2=%%P"
if not defined CAND2 goto no_python
"%CAND2%" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto no_python
set "PYEXE=%CAND2%"
echo       using the Python launcher:
"%PYEXE%" --version
goto have_python

:no_python
echo       No suitable Python found (this project needs 3.10 or newer).
echo.
echo  If you already have Python installed, it is either too old, or it was
echo  installed without ticking "Add Python to PATH".
echo  Install or upgrade to Python 3.10+ (tick that checkbox!), then run this
echo  file again.
echo.
where winget >nul 2>&1
if not %errorlevel%==0 goto manual_download
echo  Windows Package Manager is available.
set /p ANS=Try installing Python 3.12 with winget now? [Y/N] 
if /i "%ANS%"=="Y" goto winget_install
goto give_up

:winget_install
echo       Installing Python 3.12 via winget, this takes a few minutes...
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
echo.
REM The current window's PATH is not refreshed after installing, so call the
REM freshly installed interpreter by absolute path and continue setup.
set "NEWPY="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "NEWPY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined NEWPY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "NEWPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined NEWPY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "NEWPY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined NEWPY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "NEWPY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined NEWPY goto winget_reopen
set "PYEXE=%NEWPY%"
echo       Python is ready:
"%PYEXE%" --version
goto have_python

:winget_reopen
echo  Python was installed, but not where this script can call it directly.
echo  Close this window and run setup.bat again - it will pick it up.
goto give_up

:manual_download
echo  Opening the Python download page in your browser...
start "" "https://www.python.org/downloads/"

:give_up
echo.
pause
exit /b 1

:have_python
echo.
if exist "venv\Scripts\python.exe" goto have_venv
echo [2/4] Creating the virtual environment...
%PYEXE% -m venv venv
if not exist "venv\Scripts\python.exe" goto venv_failed

:have_venv
echo.
echo [3/4] Installing Python packages. This takes a few minutes...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto pip_failed
echo.
echo       Done. The next step is optional.
echo       The webchat backend drives a real browser, which needs a one-time
echo       download of about 150 MB. Skip it if you plan to use an API key.
set /p PW=Download the browser engine now? [Y/N] 
if /i "%PW%"=="Y" venv\Scripts\python.exe -m playwright install chromium

echo.
echo [4/4] Looking for Zotero...
venv\Scripts\python.exe setup_env.py

echo.
echo ============================================================
echo   Setup complete.
echo.
echo   Next: double-click start.bat to launch literature_flow.
echo ============================================================
echo.
pause
exit /b 0

:venv_failed
echo.
echo  ERROR: could not create the virtual environment.
echo  Make sure Python 3.10 or newer is installed, then run setup.bat again.
echo.
pause
exit /b 1

:pip_failed
echo.
echo  ERROR: package installation failed.
echo  Check your network or proxy settings, then run setup.bat again.
echo.
pause
exit /b 1
