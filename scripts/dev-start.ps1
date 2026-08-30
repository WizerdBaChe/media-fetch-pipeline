# Start the local API and the Vite dev server in two labelled windows.
#
# Two windows rather than one multiplexed console on purpose: when something
# fails you want to see WHICH half failed and read its own scrollback, and a
# merged stream makes a uvicorn traceback and a Vite HMR notice look alike.

[CmdletBinding()]
param(
    [switch]$NoBrowser
)

. "$PSScriptRoot\_common.ps1"

$root    = Get-ProjectRoot
$python  = Get-PythonExe
$guiDir  = Get-GuiDir
$apiPort = Get-ApiPort
$uiPort  = Get-UiPort

Assert-Python
Assert-GuiDeps

Write-Step 'Checking for anything already running'
if (Test-PortListening -Port $apiPort) {
    Write-Fail "Port $apiPort is already in use. Run scripts\dev-stop.bat first."
    Write-Host '  (Two servers on one queue.json would corrupt it -- refusing to start.)'
    exit 1
}
if (Test-PortListening -Port $uiPort) {
    Write-Fail "Port $uiPort is already in use. Run scripts\dev-stop.bat first."
    exit 1
}
Write-Ok 'Both ports are free'

Write-Step "Starting API on 127.0.0.1:$apiPort"
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', "title mfp API (port $apiPort) && `"$python`" -m mfp serve --port $apiPort" `
    -WorkingDirectory $root
if (-not (Wait-ForPort -Port $apiPort -TimeoutSeconds 30 -Label 'API')) {
    Write-Host '  The API window is still open -- read its output for the reason.'
    exit 1
}
Write-Ok "API listening on http://127.0.0.1:$apiPort"

Write-Step "Starting Vite dev server on $uiPort"
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', "title mfp GUI (port $uiPort) && npm run dev" `
    -WorkingDirectory $guiDir
if (-not (Wait-ForPort -Port $uiPort -TimeoutSeconds 60 -Label 'Vite')) {
    Write-Host '  The GUI window is still open -- read its output for the reason.'
    exit 1
}
Write-Ok "GUI listening on http://localhost:$uiPort"

Write-Host ''
Write-Host "  Open  http://localhost:$uiPort" -ForegroundColor Cyan
Write-Host '  Stop  scripts\dev-stop.bat'
Write-Host ''
Write-Host '  Note: open the GUI on localhost:5173, NOT the API port directly.' -ForegroundColor DarkGray
Write-Host '  The dev server proxies /v1; a direct cross-origin call is refused by' -ForegroundColor DarkGray
Write-Host '  the loopback guard (O-3) and returns 403.' -ForegroundColor DarkGray

if (-not $NoBrowser) {
    Start-Process "http://localhost:$uiPort"
}
