@echo off
rem fxproxy Windows one-click installer.
rem Double-click to run, or pass parameters:
rem   install.bat -Country US -Autostart
rem   install.bat -CookieFile D:\export.json -NoStart
rem   install.bat -Uninstall
setlocal
title fxproxy - Windows install
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
if "%~1"=="" pause
