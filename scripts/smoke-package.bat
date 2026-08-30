@echo off
rem Launcher for smoke-package.ps1 -- double-click safe.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0smoke-package.ps1"  %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo.& echo Exited with code %RC%.
echo.
pause
exit /b %RC%
