@echo off
rem Daily TrendPilot run — invoked by Windows Task Scheduler every weekday morning.
rem %~dp0 is this script's own folder, so the job doesn't depend on where the
rem repo happens to live.
cd /d "%~dp0"
echo ================================ >> reports\run_log.txt
echo Run started %date% %time% >> reports\run_log.txt
rem -u: unbuffered. With output redirected to a file, Python otherwise holds
rem it in a buffer, and an interrupted run lost every line it had printed.
venv\Scripts\python.exe -u -m src.main >> reports\run_log.txt 2>&1
echo Run finished %date% %time% (exit %errorlevel%) >> reports\run_log.txt
