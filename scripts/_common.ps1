# Shared helpers for the scripts in this folder.
# Dot-source it: . "$PSScriptRoot\_common.ps1"

$ErrorActionPreference = 'Stop'

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:Python      = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
$script:GuiDir      = Join-Path $script:ProjectRoot 'gui'
$script:ElectronDir = Join-Path $script:ProjectRoot 'electron'
$script:ApiPort     = 47821
$script:UiPort      = 5173

function Get-ProjectRoot { $script:ProjectRoot }
function Get-PythonExe   { $script:Python }
function Get-GuiDir      { $script:GuiDir }
function Get-ElectronDir { $script:ElectronDir }
function Get-ApiPort     { $script:ApiPort }
function Get-UiPort      { $script:UiPort }

function Write-Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  OK   $text" -ForegroundColor Green }
function Write-Skip($text) { Write-Host "  SKIP $text" -ForegroundColor DarkYellow }
function Write-Fail($text) { Write-Host "  FAIL $text" -ForegroundColor Red }

function Assert-Python {
    if (-not (Test-Path -LiteralPath $script:Python)) {
        throw "Missing .venv at $script:Python. Create it with: py -3.12 -m venv .venv"
    }
}

function Assert-GuiDeps {
    if (-not (Test-Path -LiteralPath (Join-Path $script:GuiDir 'node_modules'))) {
        throw "gui/node_modules is missing. Run: npm --prefix gui install"
    }
}

# True when something is listening on $Port. Used instead of a fixed sleep --
# a sleep either wastes time or fires early, and both look like flakiness.
function Test-PortListening([int]$Port) {
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return $null -ne $connection
    } catch {
        return $false
    }
}

function Wait-ForPort([int]$Port, [int]$TimeoutSeconds = 30, [string]$Label = 'service') {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) { return $true }
        Start-Sleep -Milliseconds 250
    }
    Write-Fail "$Label did not start listening on port $Port within $TimeoutSeconds s"
    return $false
}

# Stop whatever owns $Port. Deliberately narrow: it resolves the owning PID
# rather than killing every python.exe or node.exe on the machine, which would
# take out unrelated work the user has open.
function Stop-PortOwner([int]$Port, [string]$Label) {
    $owners = @()
    try {
        $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        Write-Skip "$Label - nothing listening on port $Port"
        return
    }

    foreach ($processId in $owners) {
        try {
            $process = Get-Process -Id $processId -ErrorAction Stop
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Ok "$Label - stopped $($process.ProcessName) (PID $processId) on port $Port"
        } catch {
            Write-Fail "$Label - could not stop PID ${processId}: $($_.Exception.Message)"
        }
    }
}
