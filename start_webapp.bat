@echo off
rem Start the dashboard: http://localhost:8787
rem Set PY to your Python 3.10+ executable if "python" is not on PATH
if "%PY%"=="" set PY=python
cd /d "%~dp0"
"%PY%" webapp.py
