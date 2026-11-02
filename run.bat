@echo off
REM ============================================================
REM  AI Visual Quality Inspection System - one-click launcher
REM  Starts: Celery worker (background window) + Django server
REM  Double-click this file, then open http://127.0.0.1:8000
REM ============================================================
setlocal
cd /d "%~dp0"
title VQI Launcher

REM ---- Pick Python: project venv first, then system Python ----
set "PY=python"
set "PIP=pip"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    set "PIP=.venv\Scripts\python.exe -m pip"
)

REM ---- First run: create venv and install dependencies ----
%PY% -c "import django, celery, PIL, numpy" >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Dependencies missing - creating .venv and installing requirements...
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python is not installed or not on PATH. Install Python 3 and retry.
        pause
        exit /b 1
    )
    python -m venv .venv || (echo [ERROR] venv creation failed.& pause & exit /b 1)
    set "PY=.venv\Scripts\python.exe"
    set "PIP=pip"
    .venv\Scripts\python.exe -m pip install --upgrade pip >nul
    .venv\Scripts\python.exe -m pip install -r requirements.txt || (echo [ERROR] pip install failed.& pause & exit /b 1)
    echo [SETUP] Dependencies installed.
)

REM ---- .env (falls back to built-in dev defaults if absent) ----
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [SETUP] Created .env from .env.example
)

REM ---- Database ----
echo [DB] Applying migrations...
%PY% manage.py migrate --noinput >nul 2>&1
if errorlevel 1 (
    echo [ERROR] migrate failed. Check the output above / settings.
    pause
    exit /b 1
)

REM ---- Optional demo data (only when the DB has no products yet) ----
%PY% -c "import os,django;os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');django.setup();from inspection.models import Product;exit(0 if Product.objects.exists() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [DB] No products found - seeding demo data...
    %PY% manage.py seed_demo
)

REM ---- Redis check (non-fatal: warn and continue) ----
%PY% -c "import socket;s=socket.create_connection(('127.0.0.1',6379),2);s.close()" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Redis is NOT reachable on 127.0.0.1:6379.
    echo [WARN] Start Redis before uploading images, otherwise Celery tasks stay queued.
)

REM ---- Celery worker in its own window (--pool=solo is the Windows-safe pool) ----
REM /D sets the working directory, so no nested quotes around the path are needed.
netstat -ano | findstr /c:":8000" | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [WARN] Port 8000 is already in use - the site is probably already running.
    echo [WARN] Opening the browser; close the other launcher window to stop it.
    echo.
    start "Opening browser" /min powershell -NoProfile -Command "Start-Process 'http://127.0.0.1:8000/'"
    pause
    exit /b 0
)

echo [WORKER] Starting Celery worker in a separate window...
start "VQI Celery Worker" /D "%~dp0" cmd /k "%PY% -m celery -A config worker -l info --pool=solo"

REM ---- Open the browser once Django has had a moment to bind the port ----
start "Opening browser" /min powershell -NoProfile -Command "Start-Sleep 6; Start-Process 'http://127.0.0.1:8000/'"

REM ---- Django development server in this window ----
echo [SERVER] Starting Django at http://127.0.0.1:8000  (Ctrl+C to stop)
echo [SERVER] The browser should open by itself in a few seconds - if not, type
echo [SERVER] http://127.0.0.1:8000 into the address bar of Chrome or Edge.
echo.
%PY% manage.py runserver 127.0.0.1:8000

REM ---- Keep the window readable if the server dies (e.g. port already used) ----
echo.
echo [INFO] Django server stopped.
pause
endlocal
