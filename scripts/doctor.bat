@echo off
rem Report which external dependencies are present (yt-dlp / gallery-dl / ffmpeg / Chrome).
setlocal
pushd "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Create it with:  py -3.12 -m venv .venv
    popd
    echo.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m mfp doctor
set "RC=%ERRORLEVEL%"
popd
echo.
pause
exit /b %RC%
