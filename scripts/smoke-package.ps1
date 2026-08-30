# Packaged smoke test (PSM G6 §11.2).
#
# Two phases, because §11.2's four assertions have two different subjects:
#
#   A. release/sidecar/mfp-sidecar.exe, driven directly. This is the
#      PyInstaller gate. uvicorn resolves its loop and protocol
#      implementations BY STRING at runtime, so a missing --hidden-import
#      produces a binary that builds cleanly and dies on first run. Nothing
#      but running it catches that, which is the entire reason this file
#      exists.
#
#   B. release/windows/*portable.exe, launched for real. This is the orphan
#      check -- §11.2 item 4, the one defect you cannot see by eye. Skipped
#      with a loud SKIP when no portable build is present.
#
# Both phases redirect %APPDATA% to a scratch directory, so the smoke can
# never take the real queue lock away from an app the user has open.

[CmdletBinding()]
param(
    [int]$ReadyTimeoutSeconds = 60,
    [int]$OrphanGraceSeconds = 5
)

. "$PSScriptRoot\_common.ps1"

$root        = Get-ProjectRoot
$sidecarExe  = Join-Path $root 'release\sidecar\mfp-sidecar.exe'
$guiDist     = Join-Path $root 'gui\dist'
$failures    = [System.Collections.Generic.List[string]]::new()

function Check($label, [bool]$ok, $detail) {
    if ($ok) { Write-Ok "$label -- $detail" }
    else { Write-Fail "$label -- $detail"; $failures.Add($label) }
}

function New-ScratchAppData {
    $dir = Join-Path $env:TEMP ("mfp-smoke-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return $dir
}

# ---------------------------------------------------------------- phase A --

Write-Step 'Phase A  mfp-sidecar.exe, driven directly'

if (-not (Test-Path -LiteralPath $sidecarExe)) {
    Write-Fail "no sidecar at $sidecarExe. Run scripts\build-all.bat first."
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $guiDist 'index.html'))) {
    Write-Fail "no renderer at $guiDist. Run scripts\build-all.bat first."
    exit 1
}

$scratch = New-ScratchAppData
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $sidecarExe
# One quoted string, not ArgumentList: `ProcessStartInfo.ArgumentList` only
# exists on .NET Core, and this script runs under Windows PowerShell 5.1 on
# .NET Framework, where it is silently $null.
$startInfo.Arguments = "serve --port 0 --ready-json --exit-on-stdin-eof --gui-dist `"$guiDist`""
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
# A real stdin PIPE, not Start-Process: closing it is how the parent-death
# path is exercised, and Start-Process cannot hand one back.
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.EnvironmentVariables['APPDATA'] = $scratch

$proc = [System.Diagnostics.Process]::Start($startInfo)
$stderrTask = $proc.StandardError.ReadToEndAsync()

try {
    # --- 1. ready line ----------------------------------------------------
    $readTask = $proc.StandardOutput.ReadLineAsync()
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $gotLine = $readTask.Wait($ReadyTimeoutSeconds * 1000)
    $sw.Stop()

    if (-not $gotLine) {
        Check '1 ready line' $false "no line on stdout within $ReadyTimeoutSeconds s"
        throw 'sidecar never announced readiness'
    }

    $ready = $null
    try { $ready = $readTask.Result | ConvertFrom-Json } catch { }
    $readyOk = $null -ne $ready -and $ready.event -eq 'ready' -and $ready.port -gt 0
    Check '1 ready line' $readyOk ("{0} after {1:N2} s" -f $readTask.Result, $sw.Elapsed.TotalSeconds)
    if (-not $readyOk) { throw 'ready line was not the expected shape' }

    $base = "http://127.0.0.1:$($ready.port)"

    # --- 2. GET /v1/health ------------------------------------------------
    $health = Invoke-WebRequest -UseBasicParsing -Uri "$base/v1/health" -TimeoutSec 10
    $healthBody = $health.Content | ConvertFrom-Json
    Check '2 GET /v1/health' ($health.StatusCode -eq 200 -and $healthBody.ok -eq $true) `
        "$($health.StatusCode) ok=$($healthBody.ok) apiVersion=$($healthBody.apiVersion)"

    # --- 3. GET / serves the SPA -----------------------------------------
    $index = Invoke-WebRequest -UseBasicParsing -Uri "$base/" -TimeoutSec 10
    Check '3 GET / serves the SPA' `
        ($index.StatusCode -eq 200 -and $index.Content -match '<div id="root">') `
        "$($index.StatusCode), $($index.RawContentLength) bytes"

    # --- 3b. the O-3 guard admits the renderer, which is the whole of §2 ---
    # The architectural decision (§2) rests on renderer and API sharing an
    # origin. If the guard refused these headers the packaged app would show
    # a shell that cannot talk to its own server, so assert it rather than
    # assume it.
    $sameOrigin = Invoke-WebRequest -UseBasicParsing -Uri "$base/v1/queue" -TimeoutSec 10 `
        -Headers @{ Origin = $base; 'Sec-Fetch-Site' = 'same-origin' }
    Check '3b same-origin /v1 call passes the O-3 guard' ($sameOrigin.StatusCode -eq 200) `
        "$($sameOrigin.StatusCode)"

    # --- 3c. and still refuses a cross-site one ---------------------------
    $crossStatus = 0
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$base/v1/queue" -TimeoutSec 10 `
            -Headers @{ Origin = 'https://evil.example'; 'Sec-Fetch-Site' = 'cross-site' } | Out-Null
    } catch {
        $crossStatus = [int]$_.Exception.Response.StatusCode
    }
    Check '3c cross-site /v1 call is refused' ($crossStatus -eq 403) "HTTP $crossStatus"

    # --- 4a. stdin EOF terminates the sidecar ------------------------------
    $proc.StandardInput.Close()
    $exited = $proc.WaitForExit(5000)
    Check '4a stdin EOF terminates the sidecar' $exited `
        $(if ($exited) { "exit code $($proc.ExitCode) within 5 s" } else { 'still running after 5 s' })
}
catch {
    Write-Fail $_.Exception.Message
    $stderrText = ''
    if ($stderrTask.Wait(2000)) { $stderrText = $stderrTask.Result }
    if ($stderrText) { Write-Host "  sidecar stderr:`n$stderrText" -ForegroundColor DarkGray }
    if (-not $failures.Contains('phase A')) { $failures.Add('phase A') }
}
finally {
    if (-not $proc.HasExited) {
        # `Process.Kill($true)` (kill the tree) is .NET Core only, and the
        # onefile bootloader means there IS a tree.
        & taskkill.exe /pid $proc.Id /t /f 2>&1 | Out-Null
    }
    Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------- phase A2 --
#
# The agent surface (O-9). Everything here was green in unit tests while the
# shipped product had no `mfp` at all, because every one of those tests ran
# against the checkout. These run the FROZEN binary.

Write-Step 'Phase A2  mfp.exe, the agent surface (O-9)'

$cliExe     = Join-Path $root 'release\sidecar\mfp.exe'
$shippedCli = Join-Path $root 'release\windows\win-unpacked\resources\sidecar\mfp.exe'

if (-not (Test-Path -LiteralPath $cliExe)) {
    Write-Fail "no CLI at $cliExe -- the Skill would teach commands that do not exist"
    $failures.Add('phase A2')
}
else {
    # 5a. The guide comes out of the binary, not off the developer's disk.
    $guide = (& $cliExe agent-guide) -join "`n"
    Check '5a agent-guide prints the contract' `
        ($guide -match 'Never loop-retry on exit 4 or exit 7') `
        ("{0:N0} chars on stdout" -f $guide.Length)

    # 5b. ...and from inside the bundle. A path resolving to a checkout would
    # pass on this machine and print nothing on the user's.
    $meta = & $cliExe agent-guide --json | ConvertFrom-Json
    Check '5b the Skill is read from the bundle' `
        ($meta.skillPath -like '*_internal\agent\SKILL.md') $meta.skillPath

    # 5c/5d. The real write path into a scratch directory -- not a dry run,
    # because what is worth checking is that a FROZEN build can still find
    # and copy its own payload.
    $skillScratch = Join-Path $env:TEMP ("mfp-smoke-skill-" + [guid]::NewGuid().ToString('N'))
    try {
        & $cliExe agent-register --target claude --path $skillScratch --json | Out-Null
        $written = Join-Path $skillScratch 'SKILL.md'
        Check '5c agent-register writes the Skill' (Test-Path -LiteralPath $written) $written

        & $cliExe agent-register --target claude --path $skillScratch --remove --json | Out-Null
        Check '5d --remove takes it back off' (-not (Test-Path -LiteralPath $written)) 'SKILL.md gone'
    }
    finally {
        Remove-Item -Recurse -Force $skillScratch -ErrorAction SilentlyContinue
    }

    # 5e. PATH is only ever previewed here. A smoke test that edited the
    # developer's PATH would be one nobody dares run twice.
    $pathBefore = [Environment]::GetEnvironmentVariable('Path', 'User')
    $preview    = & $cliExe install-path --dry-run --json | ConvertFrom-Json
    $pathAfter  = [Environment]::GetEnvironmentVariable('Path', 'User')
    Check '5e install-path --dry-run previews and writes nothing' `
        ($preview.directory -eq (Split-Path -Parent $cliExe) -and $pathBefore -eq $pathAfter) `
        "$($preview.action) -> $($preview.directory)"

    # 5f. The copy that actually reaches a user. `release\sidecar` is the
    # staging tree; this is the one inside the shell.
    Check '5f the CLI is in the packaged tree' (Test-Path -LiteralPath $shippedCli) $shippedCli

    # 5g. `mfp.brief` really loads inside the bundle.
    #
    # `brief --help` would NOT prove this: the help text comes from the
    # parser, and the module is imported inside the handler. So this calls
    # `brief-save` with a directory that does not exist, which reaches the
    # import on its first line and then fails on the path -- exit 2 with a
    # sentence about the directory means the module loaded, and a traceback
    # or any other exit code means it did not.
    #
    # It is the newest verb and the one whose imports are all inside function
    # bodies, so if `mfp.spec`'s "PyInstaller finds those on its own" rule is
    # ever wrong, this is where it shows.
    $absentDir = Join-Path $env:TEMP ("mfp-smoke-absent-" + [guid]::NewGuid().ToString('N'))
    # `2>&1` on a NATIVE exe under `$ErrorActionPreference = 'Stop'` (set in
    # `_common.ps1`) wraps every stderr line in a NativeCommandError and
    # terminates the script -- on the one check whose whole subject is a
    # refusal printed to stderr. The check could never pass, and the failure
    # named `brief-save` rather than the redirection.
    #
    # Relaxed for this one statement rather than for the file: everywhere
    # else, a native command failing IS the news.
    $briefErr = & {
        $ErrorActionPreference = 'Continue'
        (& $cliExe brief-save --post $absentDir 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ }
        }) -join "`n"
    }
    Check '5g the brief module loads in the frozen build' `
        ($LASTEXITCODE -eq 2 -and $briefErr -notmatch 'Traceback') `
        ("exit {0}: {1}" -f $LASTEXITCODE, ($briefErr -split "`n" | Select-Object -First 1))
}

# ---------------------------------------------------------------- phase B --

Write-Step 'Phase B  portable exe, orphan check (§11.2 item 4)'

$portable = Get-ChildItem -LiteralPath (Join-Path $root 'release\windows') -Filter '*portable.exe' `
    -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $portable) {
    Write-Skip 'no release\windows\*portable.exe -- run scripts\build-all.bat to include this phase'
}
else {
    $before = @(Get-Process -Name 'mfp-sidecar' -ErrorAction SilentlyContinue | ForEach-Object Id)
    $scratchB = New-ScratchAppData
    $appdataOrig = $env:APPDATA
    $env:APPDATA = $scratchB
    try {
        $app = Start-Process -FilePath $portable.FullName -PassThru -WindowStyle Minimized
    } finally {
        $env:APPDATA = $appdataOrig
    }

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $spawned = @()
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $spawned = @(Get-Process -Name 'mfp-sidecar' -ErrorAction SilentlyContinue |
            Where-Object { $before -notcontains $_.Id })
        if ($spawned.Count -gt 0) { break }
    }
    Check '4b portable app starts a sidecar' ($spawned.Count -gt 0) `
        "$($spawned.Count) mfp-sidecar.exe process(es)"

    if ($spawned.Count -gt 0) {
        # Kill the ANCESTOR, the way Task Manager would, and let the
        # stdin-EOF backstop do the rest. Killing the sidecar itself would
        # prove nothing.
        $sidecarPid = $spawned[0].Id
        $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$sidecarPid").ParentProcessId
        $parentName = (Get-Process -Id $parentId -ErrorAction SilentlyContinue).ProcessName
        if ($parentName -eq 'mfp-sidecar') {
            # The onefile bootloader. Its own parent is the Electron process.
            $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$parentId").ParentProcessId
        }
        Write-Host "       killing ancestor PID $parentId (sidecar PID $sidecarPid)" -ForegroundColor DarkGray
        Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds $OrphanGraceSeconds

        $orphans = @(Get-Process -Name 'mfp-sidecar' -ErrorAction SilentlyContinue |
            Where-Object { $before -notcontains $_.Id })
        Check "4c no orphan sidecar after $OrphanGraceSeconds s" ($orphans.Count -eq 0) `
            "$($orphans.Count) surviving process(es)"
        foreach ($o in $orphans) { Stop-Process -Id $o.Id -Force -ErrorAction SilentlyContinue }
    }

    if ($app -and -not $app.HasExited) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $scratchB -ErrorAction SilentlyContinue
}

# ----------------------------------------------------------------- verdict --

Write-Step 'Verdict'
if ($failures.Count -gt 0) {
    Write-Fail "FAILED: $($failures -join ', ')"
    exit 1
}
Write-Ok 'packaged smoke test passed'
exit 0
