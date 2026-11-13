@echo off
setlocal EnableDelayedExpansion
rem Starts the OEE Validator UI reliably for any user.

cd /d "%~dp0"

rem 1. If a local venv exists, test if it actually works for this user
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        echo Using local venv...
        call venv\Scripts\activate.bat
        goto :RUN
    ) else (
        echo [NOTICE] Existing venv belongs to another user or has permission restrictions.
    )
)

rem 2. Find a working Python for the current user
set "PY_CMD="
python --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"

if not defined PY_CMD (
    py --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py"
)

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
    echo [ERROR] No working Python found for user %USERNAME%.
    echo Please ensure Python is installed and in your PATH.
    pause
    exit /b 1
)

rem 3. If venv was broken or missing, create or recreate it for this user
if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment for %USERNAME%...
    !PY_CMD! -m venv venv
    call venv\Scripts\activate.bat
    python -m pip install --quiet --upgrade pip
    python -m pip install -r requirements.txt
    python -m pip install polars-lts-cpu
) else (
    rem If venv failed the permission test above, run with user's Python
    echo Running with %USERNAME%'s Python: !PY_CMD!
    !PY_CMD! -m streamlit run ui.py --server.address localhost --server.port 8501 --browser.gatherUsageStats false
    goto :END
)

:RUN
python -m streamlit run ui.py --server.address localhost --server.port 8501 --browser.gatherUsageStats false

:END
pause
