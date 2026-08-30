@echo off
rem Launcher for dev-stop.ps1 -- double-click safe.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-stop.ps1"  %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo.& echo Exited with code %RC%.
echo.
pause
exit /b %RC%
