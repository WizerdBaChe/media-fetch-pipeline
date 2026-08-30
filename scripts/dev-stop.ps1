# Stop the dev servers started by dev-start.
#
# Resolves the PID owning each port rather than killing python.exe / node.exe
# wholesale -- the user very likely has unrelated Python and Node work open.

[CmdletBinding()]
param()

. "$PSScriptRoot\_common.ps1"

Write-Step 'Stopping dev servers'
Stop-PortOwner -Port (Get-UiPort)  -Label 'GUI'
Stop-PortOwner -Port (Get-ApiPort) -Label 'API'

Start-Sleep -Milliseconds 400

$stillUp = @()
if (Test-PortListening -Port (Get-ApiPort)) { $stillUp += "API port $(Get-ApiPort)" }
if (Test-PortListening -Port (Get-UiPort))  { $stillUp += "GUI port $(Get-UiPort)" }

if ($stillUp.Count -gt 0) {
    Write-Fail "Still listening: $($stillUp -join ', ')"
    exit 1
}
Write-Ok 'Both ports are free'
