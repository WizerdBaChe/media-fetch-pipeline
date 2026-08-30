# Cold-start measurement for the packaged sidecar (PSM G6 §15, open question O-8).
#
# Measures wall time from Start() to the ready line on stdout, which is the
# only interval the user actually waits through: the window cannot load until
# that line arrives.
#
# **The decision this feeds**: median > 10 s => switch --onefile to --onedir.
# The script prints the verdict; it does not leave it hanging.
#
# **-Cache is not optional and is not inferred.** A onefile binary unpacks to
# %TEMP% on every launch, so the variable that matters is the OS file cache
# and the AV scan result for a 15 MB executable -- and this script cannot
# observe either. A warm number is not an answer to a cold-start question, so
# the operator states which one they are producing and it is stamped on the
# output. Guessing here would be a gate ruling on something it cannot
# determine.
#
# For a genuine cold reading: reboot, then run this before anything else.

[CmdletBinding()]
param(
    [ValidateSet('cold', 'warm')]
    [string]$Cache = 'warm',
    [int]$Runs = 3,
    [int]$FollowUpRuns = 3,
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot\_common.ps1"

$root       = Get-ProjectRoot
$sidecarExe = Join-Path $root 'release\sidecar\mfp-sidecar.exe'
$guiDist    = Join-Path $root 'gui\dist'
$THRESHOLD_SECONDS = 10

if (-not (Test-Path -LiteralPath $sidecarExe)) {
    Write-Fail "no sidecar at $sidecarExe. Run scripts\build-all.bat first."
    exit 1
}

function Measure-OneStart {
    $scratch = Join-Path $env:TEMP ("mfp-coldstart-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $scratch | Out-Null

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $sidecarExe
    $startInfo.Arguments = "serve --port 0 --ready-json --exit-on-stdin-eof --gui-dist `"$guiDist`""
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    # Scratch %APPDATA%: back-to-back runs must not fight over the queue lock,
    # and a measurement run must never touch the user's real queue.
    $startInfo.EnvironmentVariables['APPDATA'] = $scratch

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = [System.Diagnostics.Process]::Start($startInfo)
    $readTask = $proc.StandardOutput.ReadLineAsync()
    $ok = $readTask.Wait($TimeoutSeconds * 1000)
    $sw.Stop()

    $seconds = $null
    if ($ok -and $readTask.Result -match '"event"') { $seconds = $sw.Elapsed.TotalSeconds }

    try { $proc.StandardInput.Close() } catch { }
    if (-not $proc.WaitForExit(5000)) { & taskkill.exe /pid $proc.Id /t /f 2>&1 | Out-Null }
    Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
    return $seconds
}

function Get-Median([double[]]$values) {
    $sorted = $values | Sort-Object
    $n = $sorted.Count
    if ($n -eq 0) { return $null }
    if ($n % 2 -eq 1) { return $sorted[[int](($n - 1) / 2)] }
    return ($sorted[$n / 2 - 1] + $sorted[$n / 2]) / 2
}

$exe = Get-Item $sidecarExe
Write-Step "Cold-start measurement  ($Cache cache, as declared by the operator)"
Write-Host ("  binary: {0}  ({1:N0} bytes, built {2})" -f $exe.Name, $exe.Length, $exe.LastWriteTime) -ForegroundColor DarkGray
if ($Cache -eq 'warm') {
    Write-Host '  NOTE: warm cache. This is a LOWER BOUND on the real first-launch time.' -ForegroundColor DarkYellow
    Write-Host '        For the number O-8 actually asks for, reboot and re-run with -Cache cold.' -ForegroundColor DarkYellow
}

$first = @()
for ($i = 1; $i -le $Runs; $i++) {
    $s = Measure-OneStart
    if ($null -eq $s) { Write-Fail "run $i produced no ready line within $TimeoutSeconds s"; exit 1 }
    $first += $s
    Write-Host ("  run {0}: {1,7:N2} s" -f $i, $s)
}

$followUp = @()
for ($i = 1; $i -le $FollowUpRuns; $i++) {
    $s = Measure-OneStart
    if ($null -eq $s) { Write-Fail "follow-up run $i produced no ready line"; exit 1 }
    $followUp += $s
    Write-Host ("  follow-up {0}: {1,7:N2} s" -f $i, $s) -ForegroundColor DarkGray
}

$median = Get-Median $first
$followUpMedian = Get-Median $followUp

Write-Step 'Result'
Write-Host ("  cache declared : {0}" -f $Cache)
Write-Host ("  runs           : {0}" -f (($first | ForEach-Object { '{0:N2}' -f $_ }) -join ' / '))
Write-Host ("  MEDIAN         : {0:N2} s" -f $median)
Write-Host ("  follow-up med. : {0:N2} s" -f $followUpMedian)
Write-Host ("  threshold      : > {0} s => switch --onefile to --onedir" -f $THRESHOLD_SECONDS)

if ($median -gt $THRESHOLD_SECONDS) {
    Write-Fail ("VERDICT: {0:N2} s exceeds {1} s. Switch to --onedir (change --onefile in build-all.ps1, and the sidecar path + extraResources in electron/package.json)." -f $median, $THRESHOLD_SECONDS)
    exit 2
}

Write-Ok ("VERDICT: {0:N2} s is under {1} s. Keep --onefile." -f $median, $THRESHOLD_SECONDS)
if ($Cache -eq 'warm') {
    Write-Host '  ...on a WARM cache. Re-run after a reboot before treating O-8 as closed.' -ForegroundColor DarkYellow
}
exit 0
