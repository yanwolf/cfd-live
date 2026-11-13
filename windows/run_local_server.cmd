@echo off
rem Run cfd-live server locally for EA testing (ASCII only on purpose)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_local_server.ps1"
pause
