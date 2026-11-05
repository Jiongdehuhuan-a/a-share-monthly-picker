@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 将创建 Windows 计划任务：每个交易日 17:30 自动更新数据并计算信号。
echo 任务名称：A股选股助手-每日更新
echo.
schtasks /create /tn "A股选股助手-每日更新" /tr "\"%~dp0每日更新.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 17:30 /f
if %errorlevel%==0 (
  echo.
  echo [成功] 已创建。查看：schtasks /query /tn "A股选股助手-每日更新"
) else (
  echo.
  echo [失败] 需要以管理员身份运行本脚本。
)
pause
