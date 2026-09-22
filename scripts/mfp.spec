# -*- mode: python ; coding: utf-8 -*-
"""Two entry points, one `_internal` (O-9).

Until now the packaged tree carried `mfp-sidecar.exe` and nothing else, so a
machine that ran the installer had the GUI's server and no CLI -- while the
Skill shipped with the product taught agents to call `mfp probe` and
`mfp fetch`. Adding a second PyInstaller invocation would have produced a
second ~24 MB `_internal` for a dependency set that is byte-for-byte the
same; a spec with two `Analysis`/`EXE` pairs feeding ONE `COLLECT` produces
two executables sharing a single dependency tree, which is what the two of
them actually are.

`skill/SKILL.md` travels inside the bundle rather than beside the exe. That
is what makes `mfp agent-guide` answer from a packaged build with no second
copy of the file to drift, and it is why `build-all.ps1` asserts the payload
landed -- a missing data file would leave `agent-guide` printing nothing,
which is the exact failure this work exists to remove.

Kept as a versioned spec under `scripts/` because `build/` is generated and
git-ignored. Run it from the project root:

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean \\
        --distpath build\\pyinstaller-dist --workpath build\\pyinstaller \\
        scripts\\mfp.spec
"""

import glob
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
SRC = os.path.join(ROOT, "src")

# THE RULE FOR THIS LIST: only modules resolved BY STRING at runtime, which
# PyInstaller's static analysis cannot see. Anything reached by an ordinary
# import statement -- including one inside a function body -- is found on its
# own and must NOT be listed here.
#
# The rule is written down because the list had drifted from it: it carried
# `mfp.server.app`, `mfp.server.routes_queue` and `mfp.server.routes_config`,
# which are plain top-level imports in `app.py`, while `routes_stack` --
# imported exactly the same way -- was absent. Nothing was broken (verified
# 2026-08-23: the packaged `mfp.exe` carries the `stack` verb with
# `routes_stack` unlisted), but two of four routers listed with no stated
# rule leaves the next person guessing whether a fifth needs a line. It does
# not. Removed rather than completed, so the list means one thing.
#
# `uvicorn.protocols.websockets.auto` is not in PSM 9.1's list but is
# required all the same: uvicorn's default `ws="auto"` imports it during
# `Config.load()`, before a single request arrives, so its absence is a
# startup crash and not a lost capability. This product serves no
# websockets; the import still has to resolve.
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.lifespan.on",
    "uvicorn.protocols.websockets.auto",
]

# `PIL._avif` is 7.53 MB -- the single largest file in the bundle, larger than
# python312.dll -- and nothing reads AVIF. Pillow is used only by `stack.py`,
# only via Image.open / convert("L") / convert("RGB") / save() on JPEG frames
# ffmpeg produced. `webp` and `heic` appear in this product solely as DOWNLOAD
# extension allowlists, for files this process never opens. Measured saving:
# ~14% of the 54.3 MB sidecar, ~7% of the installer.
#
# Safe because Pillow registers format plugins lazily and tolerates one being
# absent -- but that is a claim about Pillow, so `build-all.ps1`'s stack smoke
# test is what actually keeps it true. If a future feature ever reads AVIF,
# delete this line rather than working around it.
EXCLUDES = ["pytest", "_pytest", "PIL._avif"]

# `agent/SKILL.md` under the bundle root -- `mfp.agent.skill_path()` reads
# exactly this path when `sys.frozen` is set, and `test_agent_surface.py`
# pins the expression.
DATAS = [
    (os.path.join(ROOT, "skill", "SKILL.md"), "agent"),
    # The 延伸工具 contracts, split out of SKILL.md in M5 (ruling R9).
    #
    # Globbed, which data files usually should not be -- shipping whatever
    # somebody drops into a directory is how an installer grows a file nobody
    # decided to include. This directory holds one kind of thing and only that kind:
    # an agent contract, every one of which must ship or `agent-guide
    # --extension` prints「the packaged guide is missing」on a user's machine
    # and nowhere else. `agent.EXTENSIONS` is the list that decides what
    # exists; a glob here just keeps the bundle from disagreeing with it.
    *[
        (path, os.path.join("agent", "extensions"))
        for path in glob.glob(os.path.join(ROOT, "skill", "extensions", "*.md"))
    ],
]

sidecar_analysis = Analysis(
    [os.path.join(SRC, "mfp", "sidecar.py")],
    pathex=[SRC],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

cli_analysis = Analysis(
    [os.path.join(SRC, "mfp", "cli.py")],
    pathex=[SRC],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

sidecar_pyz = PYZ(sidecar_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

sidecar_exe = EXE(
    sidecar_pyz,
    sidecar_analysis.scripts,
    [],
    exclude_binaries=True,
    name="mfp-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="mfp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# One COLLECT, both exes. The two dependency sets are the same package, so
# the duplicates collapse rather than doubling the tree.
coll = COLLECT(
    sidecar_exe,
    sidecar_analysis.binaries,
    sidecar_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="mfp-sidecar",
)
