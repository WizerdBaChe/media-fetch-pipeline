@echo off
rem Seed one task per state so the UI can be checked by eye (M2/M3 is frozen,
rem so nothing can reach those states legitimately yet).
rem   seed-demo.bat           seed, refusing to clobber a non-empty queue
rem   seed-demo.bat --force   seed anyway, backing the current file up first
rem   seed-demo.bat --clean   restore the backup
setlocal
pushd "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Create it with:  py -3.12 -m venv .venv
    popd
    echo.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\seed_demo.py" %*
set "RC=%ERRORLEVEL%"
popd
echo.
pause
exit /b %RC%
