@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ===================================================
echo             OEE Validator Setup and Launch
echo ===================================================
echo.

set "PY_CMD="

rem 1. Check if 'python' works
python --version >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
)

rem 2. Check if Python Launcher 'py' works
if not defined PY_CMD (
    py --version >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=py"
    )
)

rem 3. Check common Windows install paths if PATH was not set
if not defined PY_CMD (
    for %%V in (313 312 311 310) do (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
            set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        )
        if exist "%ProgramFiles%\Python%%V\python.exe" (
            set "PY_CMD=%ProgramFiles%\Python%%V\python.exe"
        )
    )
)

if not defined PY_CMD (
    echo [ERROR] Python was not found on this system.
    echo.
    echo Reasons:
    echo  1. Python is not installed yet.
    echo  2. When installing Python, "Add python.exe to PATH" was NOT checked.
    echo.
    echo How to fix:
    echo  - Download Python 3.10+ from https://www.python.org/downloads/
    echo  - Run the installer and check the box: [x] "Add python.exe to PATH"
    echo  - Restart this script or terminal.
    echo.
    pause
    exit /b 1
)

echo Found Python: !PY_CMD!
!PY_CMD! --version
echo.

rem Create virtual environment if it doesn't exist
if not exist "venv" (
    echo [1/3] Creating virtual environment (venv)...
    !PY_CMD! -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

rem Activate virtual environment
call venv\Scripts\activate.bat

rem Install or update requirements
echo [2/3] Checking and installing dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

rem Check .env file
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [NOTICE] Created a template .env file.
        echo Please edit .env with your database passwords before running!
        echo.
    )
)

echo [3/3] Starting OEE Validator UI...
echo Open your browser at http://localhost:8501
echo Press Ctrl+C in this window to stop the server.
echo.

rem Run Streamlit UI
python -m streamlit run ui.py --server.address localhost --server.port 8501 --browser.gatherUsageStats false

pause

