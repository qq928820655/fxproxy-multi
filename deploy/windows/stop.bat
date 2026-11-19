@echo off
rem Stop proxy + panel.
setlocal
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ctl.ps1" stop
if "%~1"=="-q" exit /b 0
echo.
pause
