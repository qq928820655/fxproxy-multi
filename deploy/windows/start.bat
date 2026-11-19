@echo off
rem Start proxy + panel in background, then open the panel.
setlocal
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ctl.ps1" start -Open
if "%~1"=="-q" exit /b 0
echo.
pause
