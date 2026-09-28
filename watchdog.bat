@echo off
rem Decathlon dashboard watchdog: start server if port 8787 not responding
rem (scheduled every 5 minutes by Windows Task Scheduler, task name DecathlonWatchdog)
rem MODE SWITCH: if readonly.flag exists next to this script -> start READ-ONLY mode
rem   (snapshot only, no scanning, no push). Delete the flag and restart the
rem   server (or let watchdog restart it) to return to full monitoring.
curl -s -m 5 --noproxy "*" -o NUL http://localhost:8787/ && exit /b 0
rem double-start guard: if port 8787 already listening, do nothing (mid-restart protection)
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue) { exit 1 }"
if %errorlevel%==1 exit /b 0
cd /d "%~dp0"
if exist readonly.flag (
  set DT_READONLY=1
  start "decathlon-webapp-readonly" /MIN "%PY%" webapp.py
) else (
  start "decathlon-webapp" /MIN "%PY%" webapp.py
)
