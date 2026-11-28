@echo off
rem Create a second MT5 for the live account (C:\MT5-Live) and install the EA (ASCII only on purpose)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_live_mt5.ps1"
if errorlevel 1 pause
