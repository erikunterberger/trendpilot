@echo off
rem Manual launcher for the daily TrendPilot run (Task Scheduler calls
rem venv\Scripts\pythonw.exe -m src.daily_runner directly).
rem Runs windowless in the background via src.daily_runner, which appends all
rem output to reports\run_log.txt and runs at most once per weekday.
rem   run_daily.bat           run if today's run hasn't happened yet
rem   run_daily.bat --force   run again anyway
rem   run_daily.bat --check   show whether it would run, without running
cd /d "%~dp0"
if /i "%~1"=="--check" (
    venv\Scripts\python.exe -m src.daily_runner --check
) else (
    start "" venv\Scripts\pythonw.exe -m src.daily_runner %*
)
