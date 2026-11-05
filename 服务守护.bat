@echo off
chcp 65001 >nul
rem ===================================================================
rem  Stock Picker - watchdog launcher
rem  Called by Windows Task Scheduler every 5 minutes:
rem    - port 8770 already listening -> exit (task stays Running)
rem    - port free -> start server (task stays Running while it lives)
rem  The server process is owned by Task Scheduler, so closing any
rem  terminal or editor session will NOT kill it.
rem ===================================================================
cd /d "%~dp0"

netstat -ano | findstr /C:"127.0.0.1:8770" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 exit /b 0

set "PY="
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
  set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)
if not defined PY (
  py -3 -c "import pandas" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  python -c "import pandas" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo --- python not found --- >> data\runtime\server.log
  exit /b 1
)

echo --- starting server --- >> data\runtime\server.log
"%PY%" run.py serve --port 8770 --schedule >> data\runtime\server.log 2>&1
echo --- server exited, code=%errorlevel% --- >> data\runtime\server.log
