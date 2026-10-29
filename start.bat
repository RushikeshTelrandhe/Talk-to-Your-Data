@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Talk to Your Data

set "PY=%CD%\venv\Scripts\python.exe"
set "MODEL=%~1"
if "%MODEL%"=="" set "MODEL=nemotron-3.5-bolt:e4b"

echo ============================================
echo   Talk to Your Data - starting up
echo ============================================
echo.

call :ensure_venv || goto :fail
call :ensure_deps || goto :fail
call :ensure_ollama || goto :fail
call :ensure_model || goto :fail
call :ensure_db || goto :fail

set "OLLAMA_MODEL=%MODEL%"

call :check_port
if not errorlevel 1 (
    echo [i] Port 5000 is already in use.
    echo     Opening http://localhost:5000
    start "" "http://localhost:5000"
    goto :end
)

echo.
echo [6/6] Starting app on http://localhost:5000
echo       Press Ctrl+C to stop.
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:5000'"
"%PY%" app\app.py
goto :end

rem ---------------------------------------------------------------------------
:ensure_venv
if exist "venv\Scripts\python.exe" (
    echo [1/6] Virtual environment found.
    exit /b 0
)
echo [1/6] Creating virtual environment...
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH. Install Python 3.10 or newer.
    exit /b 1
)
python -m venv venv
if errorlevel 1 (
    echo ERROR: Could not create the virtual environment.
    exit /b 1
)
exit /b 0

rem ---------------------------------------------------------------------------
:ensure_deps
echo [2/6] Checking Python dependencies...
"%PY%" -c "import flask, flask_sqlalchemy, sqlalchemy, requests" >nul 2>&1
if not errorlevel 1 (
    echo       Dependencies OK.
    exit /b 0
)
echo       Installing dependencies...
"%PY%" -m pip install --disable-pip-version-check -q -r "app\requirements.txt"
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    exit /b 1
)
echo       Dependencies installed.
exit /b 0

rem ---------------------------------------------------------------------------
:ensure_ollama
echo [3/6] Checking Ollama...
call :ollama_running
if not errorlevel 1 (
    echo       Ollama is running.
    exit /b 0
)
where ollama >nul 2>&1
if errorlevel 1 (
    echo ERROR: Ollama is not running and was not found on PATH.
    echo        Install Ollama from https://ollama.com and run this file again.
    exit /b 1
)
echo       Ollama is not running, starting it...
start "Ollama" /min cmd /c "ollama serve"
set /a tries=0
:wait_ollama
call :ollama_running
if not errorlevel 1 (
    echo       Ollama started.
    exit /b 0
)
set /a tries+=1
if !tries! geq 30 (
    echo ERROR: Ollama did not start within 30 seconds.
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto :wait_ollama

:ollama_running
curl.exe -s -m 5 -o nul http://127.0.0.1:11434/api/tags
exit /b %errorlevel%

rem ---------------------------------------------------------------------------
:ensure_model
echo [4/6] Checking model "%MODEL%"...
powershell -NoProfile -Command "if ((Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10).models.name -contains '%MODEL%') { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo       Model is available.
    exit /b 0
)
echo       Model not found. Pulling "%MODEL%" - this may take several minutes...
ollama pull "%MODEL%"
if errorlevel 1 (
    echo ERROR: Could not pull model "%MODEL%".
    exit /b 1
)
exit /b 0

rem ---------------------------------------------------------------------------
:ensure_db
echo [5/6] Checking database...
"%PY%" -c "import sqlite3,sys; c=sqlite3.connect('ecommerce.db'); sys.exit(0 if c.execute('select count(*) from orders').fetchone()[0] else 1)" >nul 2>&1
if not errorlevel 1 (
    echo       Database is ready.
    exit /b 0
)
echo       Loading data from CSV files...
"%PY%" load_data.py
if errorlevel 1 (
    echo ERROR: Could not load data into ecommerce.db.
    exit /b 1
)
exit /b 0

rem ---------------------------------------------------------------------------
:check_port
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

rem ---------------------------------------------------------------------------
:fail
echo.
echo Startup failed. See the messages above.
pause
exit /b 1

:end
endlocal
