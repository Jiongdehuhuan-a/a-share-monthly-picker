@echo off
chcp 65001 >nul
cd /d "%~dp0"

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
if not defined PY exit /b 1

%PY% run.py update
%PY% run.py signal
