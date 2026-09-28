@echo off
rem Decathlon dashboard READ-ONLY mode: serve last snapshot, no scanning, no push
rem (one-shot scheduled task: DecathlonReadonly; full monitoring = DecathlonWatchdog)
curl -s -m 5 --noproxy "*" -o NUL http://localhost:8787/ && exit /b 0
rem double-start guard: if port 8787 already listening, do nothing
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue) { exit 1 }"
if %errorlevel%==1 exit /b 0
cd /d "%~dp0"
set DT_READONLY=1
start "decathlon-webapp-readonly" /MIN "%PY%" webapp.py
