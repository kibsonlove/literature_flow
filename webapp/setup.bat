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
if %errorlevel%==0 goto use_python
where py >nul 2>&1
if %errorlevel%==0 goto use_py
goto no_python

:use_python
set "PYEXE=python"
echo       using the system Python:
python --version
goto have_python

:use_py
set "PYEXE=py"
echo       using the Python launcher:
py --version
goto have_python

:no_python
echo       Python was NOT found on this computer.
echo.
echo  Please install Python 3.10 or newer, then run this file again.
echo  During installation, remember to tick "Add Python to PATH".
echo.
where winget >nul 2>&1
if not %errorlevel%==0 goto manual_download
echo  Windows Package Manager is available.
set /p ANS=Try installing Python 3.12 with winget now? [Y/N] 
if /i "%ANS%"=="Y" goto winget_install
goto give_up

:winget_install
winget install --id Python.Python.3.12 -e
echo.
echo  When the installer is finished, close this window and run setup.bat again.
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
