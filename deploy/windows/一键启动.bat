@echo off
rem fxproxy one-click start: bring up proxy + panel in background, then open the panel.
rem Both services run on pythonw.exe, so no console window stays on screen.
setlocal
title fxproxy - start
cd /d "%~dp0"
set "CTL=%~dp0deploy\windows\ctl.ps1"
if not exist "%CTL%" set "CTL=%~dp0ctl.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" start -Open
if "%~1"=="-q" exit /b 0
echo.
echo --- Press any key to close this window. Services keep running. ---
pause >nul
