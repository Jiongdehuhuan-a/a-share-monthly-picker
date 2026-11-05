@echo off
chcp 65001 >nul
title A股选股助手
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
if not defined PY (
  echo [错误] 找不到可用的 Python，请先安装 Python 3.10+ 并 pip install pandas numpy pyarrow matplotlib
  pause
  exit /b 1
)

echo 正在启动选股助手...
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8770"
%PY% run.py serve --port 8770 --schedule
pause
