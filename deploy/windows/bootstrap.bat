@echo off
rem fxproxy Windows online one-click installer.
rem Downloads bootstrap.ps1 from the repo, then runs it (which pulls the release zip).
rem   bootstrap.bat                        default install
rem   bootstrap.bat -Country US -Shortcut  args are forwarded to bootstrap.ps1
rem   bootstrap.bat -Uninstall
setlocal
title fxproxy - online install
set "URL=https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/windows/bootstrap.ps1"
set "PS1=%TEMP%\fxproxy-bootstrap.ps1"
if not "%FXPROXY_BOOTSTRAP_URL%"=="" set "URL=%FXPROXY_BOOTSTRAP_URL%"

echo [fxproxy] fetching bootstrap.ps1 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%URL%' -OutFile '%PS1%' -UseBasicParsing } catch { Write-Host ('    X    ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
if errorlevel 1 goto :nofetch
if not exist "%PS1%" goto :nofetch

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
if "%~1"=="" pause
exit /b 0

:nofetch
echo.
echo [fxproxy] cannot reach raw.githubusercontent.com
echo     Use a mirror, or download the release zip and run install.bat:
echo     https://github.com/qq928820655/fxproxy-multi/releases/latest
pause
exit /b 1

:failed
echo.
echo [fxproxy] install failed with exit code %RC%, see messages above.
pause
exit /b 1
