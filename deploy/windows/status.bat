@echo off
rem Show process / port / quota status and recent log lines.
setlocal
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ctl.ps1" status
if "%~1"=="-q" exit /b 0
echo.
pause
