# scripts/

Double-clickable helpers. Every `.bat` is a thin launcher for the `.ps1` beside
it — edit the `.ps1`, never the `.bat`. Shared helpers live in `_common.ps1`.

| Script | What it does | Works today? |
|---|---|---|
| `dev-start.bat` | Starts the API (`mfp serve`, port 47821) and the Vite dev server (5173) in two labelled windows, waits for both to listen, opens the browser | ✅ |
| `dev-stop.bat` | Stops both, by resolving the PID that owns each port | ✅ |
| `test-all.bat` | pytest (with coverage) + `npm run check` in `gui/` and `electron/`; all three run even if an earlier one fails | ✅ |
| — | `test-all` and `watch-tests` are **not in the public tree**: it ships the program without the suites, and `build-all` skips its test stage there and says so | |
| `watch-tests.bat` | Watches `src/`, `tests/` and `gui/src/`; re-runs only the suite whose tree changed | ✅ |
| `build-all.bat` | Renderer → sidecar → desktop shell, with a summary naming what was built and what was skipped | ✅ |
| `smoke-package.bat` | Runs the PACKAGED artifacts and asserts they work (G6 §11.2) | ✅ |
| `measure-coldstart.bat` | Times the packaged sidecar from launch to ready, three runs, and applies the 10 s `--onefile`/`--onedir` threshold (O-8) | ✅ |
| `generate-icon.bat` | Regenerates `electron/build/icon.png` from code | ✅ |
| `doctor.bat` | `mfp doctor` — which external tools are installed | ✅ |
| `seed-demo.bat` | Seeds one task per state so the UI can be checked by eye; `--promote` reaches DOWNLOADING, `--clean` undoes it | ✅ |

## Building the desktop app

```powershell
npm --prefix gui install
npm --prefix electron install
.venv\Scripts\python.exe -m pip install pyinstaller
scripts\build-all.bat          # -> release\windows\media-fetch-pipeline-<v>-portable.exe
scripts\smoke-package.bat      # prove the artifacts actually run
```

## Notes that will save you a debugging session

**Open the GUI on `localhost:5173`, not the API port.** The dev server proxies
`/v1`. Hitting `127.0.0.1:47821` from a page served elsewhere is a cross-origin
request, and the loopback guard (O-3) answers `403 cross_origin_denied` — that
is the guard working, not a bug.

**`dev-start` refuses to start if port 47821 is already in use.** Two servers
would write the same `queue.json`. Run `dev-stop.bat` first.

**`build-all` still skips forward rather than failing.** Stage 2 needs
PyInstaller (`.venv\Scripts\python.exe -m pip install pyinstaller`) and stage 3
needs `electron/node_modules`. A missing input SKIPS the stage and says so in
the summary; it does not fail and does not pretend.

**`dev-start` and the desktop shell cannot both run.** Since G6 the server
takes an exclusive `queue.lock` beside `queue.json`, so the second one to start
exits with `queue_locked` naming the PID of the first. That is the guard
working: two processes writing one `queue.json` is corruption. Run
`dev-stop.bat` first.

**Both the shell and the sidecar are unsigned.** SmartScreen shows "Windows
protected your PC" on the portable exe's first run. Expected, not a defect —
see `docs/psm-batch2-g6-electron.md` §10.3.

**`seed-demo` is two steps, and INV-4 is why.** A task recorded as PROBING or
DOWNLOADING is demoted to PAUSED the moment the queue loads, so those two states
cannot be seeded through the file at all. Seed, start the server, then
`--promote` transitions them through the real API — which also proves the
transitions are legal. Seeded progress bytes survive, so promoted rows show
realistic speed and ETA rather than zeroes.

**`watch-tests` needs no extra install.** It uses .NET's `FileSystemWatcher`
rather than adding `pytest-watcher`. `-Only python` or `-Only gui` narrows it.

## PowerShell execution policy

The `.bat` launchers pass `-ExecutionPolicy Bypass`, so a restricted machine
policy will not block them. Running a `.ps1` directly may need
`powershell -ExecutionPolicy Bypass -File <script>`.
