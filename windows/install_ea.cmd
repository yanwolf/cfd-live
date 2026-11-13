@echo off
rem Install TzujenBridge EA into every MT5 on this PC (ASCII only on purpose)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_ea.ps1"
if errorlevel 1 pause
