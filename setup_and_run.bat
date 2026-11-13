@echo off
cd /d "%~dp0"

echo ===================================================
echo          OEE Validator - Setup and Launch
echo ===================================================
echo.

REM ---- Find Python ----
set "PY_CMD="

python --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"

if not defined PY_CMD (
    py --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py"
)

if not defined PY_CMD (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)
if not defined PY_CMD (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY_CMD (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
)
if not defined PY_CMD (
    if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
)

if not defined PY_CMD (
    echo [ERROR] Python not found on this system.
    echo.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo Found Python: %PY_CMD%
%PY_CMD% --version
echo.

REM ---- Create venv if not present ----
if exist "venv\Scripts\activate.bat" goto activate

echo [1/3] Creating virtual environment...
%PY_CMD% -m venv venv
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)
echo Done.
echo.

:activate
echo Activating virtual environment...
call venv\Scripts\activate.bat

REM ---- Install dependencies ----
echo [2/3] Installing / updating dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
python -m pip install --quiet polars-lts-cpu
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo Done.
echo.

REM ---- Create .env if missing ----
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [NOTICE] Created .env from template. Edit it with your DB passwords.
        echo.
    )
)

REM ---- Launch Streamlit ----
echo [3/3] Starting OEE Validator UI...
echo.
echo   Browser URL: http://localhost:8501
echo   Press Ctrl+C here to stop the server.
echo.
python -m streamlit run ui.py --server.address localhost --server.port 8501 --browser.gatherUsageStats false

pause
