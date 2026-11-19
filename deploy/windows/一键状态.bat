@echo off
rem fxproxy one-click status: live view, refreshes every 3 seconds.
rem Closing this window (or Ctrl+C) does NOT stop the background services.
setlocal
title fxproxy - live status
cd /d "%~dp0"
set "CTL=%~dp0deploy\windows\ctl.ps1"
if not exist "%CTL%" set "CTL=%~dp0ctl.ps1"
if "%~1"=="-q" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" status
  exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" status -Watch -Interval 3
echo.
echo --- Press any key to close. The services are NOT affected. ---
if "%~1"=="" pause >nul
