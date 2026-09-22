# Full release pipeline: renderer -> sidecar -> desktop shell.
#
# All four stages are live as of G6. The stage gating stays: a missing input
# SKIPS the stage and says so in the summary rather than failing silently or
# pretending. A build script that quietly produces less than expected is how a
# half-built release ships.

[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipSidecar,
    [switch]$SkipShell
)

. "$PSScriptRoot\_common.ps1"

$root        = Get-ProjectRoot
$python      = Get-PythonExe
$guiDir      = Get-GuiDir
$electronDir = Get-ElectronDir

$sidecarDist = Join-Path $root 'release\sidecar'
$pyiWork     = Join-Path $root 'build\pyinstaller'
$pyiStage    = Join-Path $root 'build\pyinstaller-dist'
# Two entry points now, so the arguments live in a versioned spec rather
# than in this file: `scripts\mfp.spec` builds `mfp-sidecar.exe` (the GUI's
# server) and `mfp.exe` (the CLI the Skill teaches agents to call) into ONE
# `_internal`. Before O-9 only the sidecar shipped, and its parser offers
# `serve` and nothing else -- so an installed machine had no `mfp probe` for
# the Skill to be about.
$spec        = Join-Path $PSScriptRoot 'mfp.spec'

$summary = [System.Collections.Generic.List[object]]::new()
function Add-Result($stage, $status, $detail) {
    $summary.Add([pscustomobject]@{ Stage = $stage; Status = $status; Detail = $detail })
}

# --- stage 0: tests -------------------------------------------------------

$testRunner = Join-Path $PSScriptRoot 'test-all.ps1'
if ($SkipTests) {
    Add-Result '0 tests' 'SKIP' '-SkipTests requested'
} elseif (-not (Test-Path -LiteralPath $testRunner)) {
    # The public mirror ships the program without the suites, and a missing
    # INPUT skips its stage and says so -- the rule this script opens with.
    # Failing here instead would make the published build script unrunnable by
    # its own documented default.
    Write-Step 'Stage 0/4  tests'
    Write-Skip 'no test-all.ps1 -- this tree ships without the test suites'
    Add-Result '0 tests' 'SKIP' 'no test runner in this tree'
} else {
    Write-Step 'Stage 0/4  tests'
    & $testRunner
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'Tests failed. Not building a release from a red tree.'
        exit 1
    }
    Add-Result '0 tests' 'OK' 'python + gui + electron green'
}

# --- stage 1: renderer ----------------------------------------------------

Write-Step 'Stage 1/4  renderer (gui -> gui/dist)'
if (-not (Test-Path -LiteralPath (Join-Path $guiDir 'node_modules'))) {
    Write-Fail 'gui/node_modules missing. Run: npm --prefix gui install'
    exit 1
}
Push-Location $guiDir
try {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw "vite build failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }

$distIndex = Join-Path $guiDir 'dist\index.html'
if (-not (Test-Path -LiteralPath $distIndex)) {
    throw "vite reported success but $distIndex does not exist"
}
$distSize = (Get-ChildItem -Recurse -File (Join-Path $guiDir 'dist') |
    Measure-Object -Property Length -Sum).Sum
Write-Ok ("gui/dist built ({0:N0} bytes)" -f $distSize)
Add-Result '1 renderer' 'OK' ("gui/dist, {0:N0} bytes" -f $distSize)

# --- stage 2: sidecar -----------------------------------------------------

Write-Step 'Stage 2/4  sidecar (PyInstaller -> release/sidecar)'
# Probe by file, not by `python -c "import PyInstaller"`. Redirecting a native
# command's stderr in Windows PowerShell 5.1 turns every stderr line into a
# terminating NativeCommandError, so the import probe fails the whole script
# precisely when the answer is "not installed" -- the case it exists to detect.
$pyinstallerPresent = Test-Path -LiteralPath (Join-Path $root '.venv\Scripts\pyinstaller.exe')

if ($SkipSidecar) {
    Write-Skip '-SkipSidecar requested'
    Add-Result '2 sidecar' 'SKIP' '-SkipSidecar requested'
} elseif (-not $pyinstallerPresent) {
    Write-Skip 'PyInstaller is not installed in .venv'
    Write-Host '       Install with:  .venv\Scripts\python.exe -m pip install pyinstaller' -ForegroundColor DarkGray
    Add-Result '2 sidecar' 'SKIP' 'PyInstaller not installed (G6 not started)'
} else {
    New-Item -ItemType Directory -Force -Path $sidecarDist, $pyiWork, $pyiStage | Out-Null
    Push-Location $root
    try {
        # onedir, not onefile (overturns D-52), and every hidden import and
        # exclusion now lives in the spec beside the reason for it.
        #
        # A onefile binary unpacks ~24 MB into %TEMP%\_MEIxxxxxx on EVERY
        # launch. Measured 2026-08-18: it cleans up on a graceful exit and
        # ORPHANS the directory on taskkill /F -- which is UAT item 11's own
        # scenario, and 42 of them (522 MB) had accumulated on the dev
        # machine. The uninstaller cannot sweep them: `_MEI*` is the name
        # EVERY PyInstaller onefile program uses, so removing them would
        # delete other programs' state. onedir does not extract at all, so
        # the leak has no source.
        & $python -m PyInstaller `
            --noconfirm --clean `
            --distpath $pyiStage --workpath $pyiWork `
            $spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
    } finally { Pop-Location }

    # Flatten the onedir output into release/sidecar so the exe keeps the
    # path everything already uses, with `_internal` beside it.
    # The COLLECT is named `mfp-sidecar`, so the staged tree keeps that name
    # even though it now holds both executables.
    $staged = Join-Path $pyiStage 'mfp-sidecar'
    if (-not (Test-Path -LiteralPath (Join-Path $staged 'mfp-sidecar.exe'))) {
        throw "PyInstaller reported success but $staged\mfp-sidecar.exe is missing"
    }
    Remove-Item -LiteralPath $sidecarDist -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $sidecarDist | Out-Null
    Copy-Item -Path (Join-Path $staged '*') -Destination $sidecarDist -Recurse -Force

    $exe = Join-Path $sidecarDist 'mfp-sidecar.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw "flatten step did not produce $exe" }
    # `_internal` is what makes onedir onedir. Without it the exe is present
    # and dead, which would pass a Test-Path check and fail on first launch.
    if (-not (Test-Path -LiteralPath (Join-Path $sidecarDist '_internal'))) {
        throw "onedir output has no _internal directory next to $exe"
    }
    # The CLI half of O-9. Asserted rather than assumed: the shell only ever
    # launches the sidecar, so a spec that quietly stopped emitting `mfp.exe`
    # would ship a green build whose Skill teaches a command that is not
    # there -- which is precisely the state this work was opened to fix.
    $cliExe = Join-Path $sidecarDist 'mfp.exe'
    if (-not (Test-Path -LiteralPath $cliExe)) {
        throw "the spec produced no mfp.exe -- the agent-facing CLI would not ship"
    }
    # ...and the Skill it prints. `mfp agent-guide` reads this exact path,
    # so a missing payload is an empty guide rather than a crash.
    $skillPayload = Join-Path $sidecarDist '_internal\agent\SKILL.md'
    if (-not (Test-Path -LiteralPath $skillPayload)) {
        throw 'the bundle carries no _internal\agent\SKILL.md -- mfp agent-guide would print nothing'
    }
    # ...and one file per 延伸工具, since M5 split them out of SKILL.md. The
    # failure guarded against -- `agent-guide --extension` reporting "the
    # packaged guide is missing" -- happens on a user's machine and nowhere
    # else, so a count check would be worthless: it would pass a bundle
    # carrying four wrong files.
    #
    # The set is READ from the source directory rather than listed here. A
    # list in this script would be a third copy of it (beside
    # `agent.EXTENSIONS` and the directory itself) and the one nobody updates.
    # `test_every_extension_is_named_in_the_core_contract` is what keeps the
    # directory and `agent.EXTENSIONS` in agreement.
    $extensionNames = Get-ChildItem -LiteralPath (Join-Path $root 'skill\extensions') -Filter '*.md' |
        ForEach-Object { $_.BaseName }
    if (-not $extensionNames) {
        throw 'skill\extensions is empty -- the 延伸工具 contracts would not ship'
    }
    foreach ($name in $extensionNames) {
        $guide = Join-Path $sidecarDist "_internal\agent\extensions\$name.md"
        if (-not (Test-Path -LiteralPath $guide)) {
            throw "the bundle carries no _internal\agent\extensions\$name.md -- mfp agent-guide --extension $name would fail"
        }
    }
    $exeSize = (Get-Item $exe).Length
    $cliSize = (Get-Item $cliExe).Length
    $treeSize = (Get-ChildItem $sidecarDist -Recurse -File | Measure-Object Length -Sum).Sum
    Write-Ok ("mfp-sidecar + mfp (onedir) built ({0:N0} + {1:N0} bytes exe, {2:N0} bytes total)" -f $exeSize, $cliSize, $treeSize)
    Add-Result '2 sidecar' 'OK' ("release/sidecar/ onedir, {0:N0} B sidecar / {1:N0} B cli / {2:N0} B total" -f $exeSize, $cliSize, $treeSize)
}

# --- stage 3: desktop shell ----------------------------------------------

Write-Step 'Stage 3/4  desktop shell (electron-builder -> release/windows)'
$shellPresent = Test-Path -LiteralPath (Join-Path $electronDir 'package.json')
$shellDeps    = Test-Path -LiteralPath (Join-Path $electronDir 'node_modules')

if ($SkipShell) {
    Write-Skip '-SkipShell requested'
    Add-Result '3 shell' 'SKIP' '-SkipShell requested'
} elseif (-not $shellPresent) {
    Write-Skip 'electron/ does not exist'
    Write-Host '       Spec: docs\psm-batch2-g6-electron.md' -ForegroundColor DarkGray
    Add-Result '3 shell' 'SKIP' 'electron/ missing'
} elseif (-not $shellDeps) {
    Write-Skip 'electron/node_modules missing. Run: npm --prefix electron install'
    Add-Result '3 shell' 'SKIP' 'electron/node_modules not installed'
} elseif (-not (Test-Path -LiteralPath (Join-Path $sidecarDist 'mfp-sidecar.exe'))) {
    Write-Skip 'no sidecar to package -- stage 2 did not produce one'
    Add-Result '3 shell' 'SKIP' 'stage 2 produced no sidecar'
} else {
    Push-Location $electronDir
    try {
        & npm.cmd run package:win
        if ($LASTEXITCODE -ne 0) { throw "electron-builder failed (exit $LASTEXITCODE)" }
    } finally { Pop-Location }

    # Name the artifacts rather than reporting the directory: "release/windows"
    # is true whether the exe was produced or left over from last week.
    #
    # BOTH are required. Two shells ship deliberately -- the installer for
    # daily use because portable pays ~5.2 s of self-extraction on every
    # launch, the portable one for running from a stick. A build that quietly
    # produced one of them would look like a pass while shipping the slow half
    # only.
    $shells = @(
        @{ Label = 'portable'; Filter = '*portable.exe' }
        @{ Label = 'setup';    Filter = '*setup.exe' }
    )
    $detail = @()
    foreach ($shell in $shells) {
        $found = Get-ChildItem -LiteralPath (Join-Path $root 'release\windows') -Filter $shell.Filter -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $found) {
            throw ("electron-builder reported success but no {0} matched '{1}'" -f $shell.Label, $shell.Filter)
        }
        Write-Ok ("{0} built ({1:N0} bytes)" -f $found.Name, $found.Length)
        $detail += ("{0} {1:N0} B" -f $shell.Label, $found.Length)
    }
    Add-Result '3 shell' 'OK' ($detail -join ' / ')
}

# --- stamp ----------------------------------------------------------------

# What commit is in release/windows? Before this, the only evidence was a file
# date, which answers "when" and not "what" -- and D-100 is the rule that a
# verified feature not in release/ is not delivered, which needs "what".
#
# Written even for a PARTIAL build, carrying the skips: a stamp that only
# appeared on perfect runs would be missing exactly when the question matters.
# It is a REPORT, never a gate: during development the stamp is behind HEAD
# almost always, and a check that is red 95% of the time gets ignored.
$stampDir = Join-Path $root 'release\windows'
if (Test-Path -LiteralPath $stampDir) {
    $head   = (& git -C $root rev-parse HEAD 2>$null)
    $branch = (& git -C $root rev-parse --abbrev-ref HEAD 2>$null)
    $dirty  = @(& git -C $root status --porcelain 2>$null).Count
    $stamp  = [ordered]@{
        commit      = if ($LASTEXITCODE -eq 0 -and $head) { $head.Trim() } else { $null }
        branch      = if ($branch) { $branch.Trim() } else { $null }
        # A build from a dirty tree is not reproducible from its commit, and
        # saying so is cheaper than discovering it later.
        dirtyPaths  = $dirty
        builtAt     = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
        stages      = @($summary | ForEach-Object {
            [ordered]@{ stage = $_.Stage; status = $_.Status; detail = $_.Detail }
        })
    }
    $stampPath = Join-Path $stampDir 'BUILD.json'
    [System.IO.File]::WriteAllText(
        $stampPath,
        (ConvertTo-Json $stamp -Depth 5),
        (New-Object System.Text.UTF8Encoding $false)
    )
    Write-Ok ("stamped {0} ({1})" -f $stampPath, $(if ($stamp.commit) { $stamp.commit.Substring(0, 7) } else { 'no git' }))
}

# --- stage 4: report ------------------------------------------------------

Write-Step 'Stage 4/4  summary'
$summary | Format-Table -AutoSize | Out-String | Write-Host

$skipped = @($summary | Where-Object { $_.Status -eq 'SKIP' })
if ($skipped.Count -gt 0) {
    Write-Host '  This was a PARTIAL build. Skipped:' -ForegroundColor DarkYellow
    foreach ($row in $skipped) { Write-Host "    - $($row.Stage): $($row.Detail)" -ForegroundColor DarkYellow }
    Write-Host ''
    Write-Host '  Usable today: run scripts\dev-start.bat and open the browser.' -ForegroundColor DarkGray
} else {
    Write-Ok 'Full pipeline complete -- see release\windows'
}
