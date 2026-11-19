@echo off
rem fxproxy one-click stop: kill the proxy and panel processes.
setlocal
title fxproxy - stop
cd /d "%~dp0"
set "CTL=%~dp0deploy\windows\ctl.ps1"
if not exist "%CTL%" set "CTL=%~dp0ctl.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" stop
if "%~1"=="-q" exit /b 0
echo.
echo --- Press any key to close this window. ---
pause >nul
