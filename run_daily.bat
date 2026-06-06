@echo off
REM Windows 작업 스케줄러가 호출하는 진입점
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist logs mkdir logs
python run_scheduled.py >> "logs\scheduler_%date:~0,4%%date:~5,2%%date:~8,2%.log" 2>&1
