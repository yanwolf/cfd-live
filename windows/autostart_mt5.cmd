@echo off
rem Add demo and live MT5 to Windows startup (ASCII only on purpose)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart_mt5.ps1"
if errorlevel 1 pause
