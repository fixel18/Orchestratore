@echo off
cd /d "%~dp0"
if not exist "log" mkdir "log"
py -3.12 orchestratore.py %* >> "log\console.log" 2>&1
