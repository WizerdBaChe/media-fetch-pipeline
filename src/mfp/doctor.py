"""Environment doctor: locate and version-check yt-dlp, ffmpeg, a JavaScript
runtime, Chrome and the recognition engine (PSM Batch 1 core M1 acceptance,
§12).

Detection only. This module never launches Chrome -- Chrome's presence and
version are determined by reading the filesystem (its versioned install
subdirectory) and the registry, matching the project-wide hard prohibition
on starting the Chrome process outside the (out-of-scope) adapter path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import winreg
from pathlib import Path
from typing import Callable

from pydantic import Field, ValidationError

from mfp.config import AppConfig, ChromeConfig, app_data_dir
from mfp.errors import ChromeDefaultProfileError
from mfp.models import CamelModel

_VERSION_RUNNER_TIMEOUT_S = 10.0

# Injectable subprocess runner type, for testability without invoking real
# binaries.
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    # argv-array invocation only -- never a shell string (Phase 2 §4.2).
    return subprocess.run(
        argv,
        capture_output=True,
        # DEVNULL, not inherited. Measured 2026-08-17: with `mfp serve
        # --exit-on-stdin-eof` -- how the Electron shell launches the sidecar
        # -- every `yt-dlp --version` probe timed out at 10 s and the whole
        # report came back exit 6 with yt-dlp "found but failed to run". The
        # same binary answers in 0.52 s from a shell. A child that inherits a
        # stdin another thread is already blocked reading is a child that can
        # hang; none of these probes has any use for our stdin.
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=_VERSION_RUNNER_TIMEOUT_S,
        shell=False,
    )


#: Dependencies the product cannot work without. Everything else is a
#: capability that degrades rather than a prerequisite that fails.
#:
#: `gallery-dl` is not here, and since 2026-08-30 it is not checked AT ALL.
#: It was designed in as the image-first engine (phase1-prior-art-sweep.md
#: H2: 「圖片為主站點（IG carousel）的補位引擎」), then put first in §12's
#: abandonment order, and then in fact abandoned -- `cli.py` refuses
#: `--platform gallerydl` outright. Nothing in this product invokes it, so
#: installing it changes no behaviour whatsoever, and a row reading
#: 「未安裝（選用）」 was telling every user they were missing something that
#: would do nothing for them. R6-5's lesson one step further: a check the
#: reader can do nothing useful about is a check that teaches them to stop
#: reading this report.
#:
#: PUT IT BACK when a new image-first platform is supported that (1) yt-dlp
#: does not cover, (2) needs no login -- D-2 is absolute, and gallery-dl's
#: Instagram path wants `--cookies-from-browser`, which is why its original
#: purpose was unreachable here -- and (3) has a maintained gallery-dl
#: extractor. None of Instagram / Threads / YouTube / X / Bilibili qualifies.
REQUIRED_BINARIES: frozenset[str] = frozenset({"yt-dlp", "ffmpeg", "chrome"})


class DoctorCheck(CamelModel):
    name: str
    found: bool
    path: str | None = None
    version: str | None = None
    ok: bool
    error_code: str | None = None
    detail: str | None = None
    #: False for a dependency whose absence degrades a capability instead of
    #: breaking the product. Such a check may be `ok=False` without failing
    #: the report.
    required: bool = True
    # "unpinned" | "ok" | "below_minimum" | "unknown" | None. None for
    # checks with no deps.lock.json entry (currently only "chrome" -- the
    # lock file only covers the three subprocess-invoked binaries).
    # Populated by _version_status() below; enforcement (failing the
    # check on "below_minimum") lands with M7, this milestone only reports.
    version_status: str | None = None


class DoctorReport(CamelModel):
    schema_version: int = 1
    ok: bool
    checks: list[DoctorCheck]
    exit_code: int


# --- deps.lock.json (PSM §3 repo layout; enforcement lands with M7) --------


class DepsLockEntry(CamelModel):
    pinned_version: str | None = None
    min_version: str | None = None


class DepsLock(CamelModel):
    schema_version: int = 1
    yt_dlp: DepsLockEntry = Field(default_factory=DepsLockEntry)
    gallery_dl: DepsLockEntry = Field(default_factory=DepsLockEntry)
    ffmpeg: DepsLockEntry = Field(default_factory=DepsLockEntry)


def _frozen_resource_roots() -> list[Path]:
    """Where a PyInstaller build keeps files shipped alongside the binary.

    Two locations, and both are needed:

    - `sys._MEIPASS` is the onefile unpack directory, which is where
      anything added with `--add-data` lands.
    - `Path(sys.executable).parent` is the directory the .exe actually sits
      in. G6 ships `deps.lock.json` through electron-builder's
      `extraResources`, not through PyInstaller, so at runtime it is a
      sibling of `mfp-sidecar.exe` under `resources/`, one level above the
      `sidecar/` folder holding the exe.

    Empty when the process is not frozen, so the dev walk below stays the
    only answer in a source checkout.
    """
    if not getattr(sys, "frozen", False):
        return []
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    exe_dir = Path(sys.executable).resolve().parent
    roots.extend([exe_dir, exe_dir.parent])
    return roots


def default_deps_lock_path() -> Path:
    """Locate `deps.lock.json`, frozen layout first (G6 §9.1/§15).

    The dev answer -- two levels up from `src/mfp/doctor.py` -- is correct
    in a source checkout and MEANINGLESS in a packaged build, where that
    walk lands somewhere inside the PyInstaller unpack directory. Left
    alone it made the packaged Doctor report `versionStatus: null` on every
    binary while looking exactly like a project that had pinned nothing:
    a wrong answer delivered in the format of a right one.

    Returns the first candidate that exists; falls back to the dev path so
    the return type stays a single Path and `load_deps_lock()` keeps its
    "missing file is not a crash" contract.
    """
    dev_path = Path(__file__).resolve().parents[2] / "deps.lock.json"
    for root in _frozen_resource_roots():
        candidate = root / "deps.lock.json"
        if candidate.is_file():
            return candidate
    return dev_path


def load_deps_lock(path: Path | None = None) -> DepsLock | None:
    """Load `deps.lock.json`. Returns `None` (never raises) when the file
    is missing, unreadable, or invalid -- version-pin enforcement is not
    yet wired (lands with M7), so its absence or corruption must never
    break `doctor`."""
    p = path or default_deps_lock_path()
    if not p.exists():
        return None
    try:
        # utf-8-sig for the same reason as config.json: a BOM here silently
        # disables version pinning, and the swallowing is by design above.
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return DepsLock.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


_VERSION_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")


def _parse_version_tuple(version: str) -> tuple[int, ...] | None:
    match = _VERSION_NUMBER_RE.search(version)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _version_status(detected_version: str | None, entry: DepsLockEntry | None) -> str | None:
    """Report-only comparison of `detected_version` against
    `entry.min_version` (PSM §3/§12 M7 follow-up). Never raises and never
    fails a check on its own in this milestone."""
    if entry is None:
        return None
    if entry.min_version is None:
        # Covers both "nothing pinned yet" and "only pinnedVersion is
        # recorded, no floor to enforce" -- both report as "unpinned".
        return "unpinned"
    if detected_version is None:
        return "unknown"
    detected_tuple = _parse_version_tuple(detected_version)
    min_tuple = _parse_version_tuple(entry.min_version)
    if detected_tuple is None or min_tuple is None:
        return "unknown"
    return "ok" if detected_tuple >= min_tuple else "below_minimum"


def _parse_first_line(output: str) -> str | None:
    if not output or not output.strip():
        return None
    return output.strip().splitlines()[0].strip() or None


def _parse_ffmpeg_version(output: str) -> str | None:
    # "ffmpeg version 6.1.1-full_build ..." -> "6.1.1-full_build"
    match = re.search(r"ffmpeg version (\S+)", output)
    if match:
        return match.group(1)
    return _parse_first_line(output)


def _check_subprocess_binary(
    name: str,
    *,
    configured_path: str | None,
    version_argv_tail: list[str],
    version_parser: Callable[[str], str | None],
    runner: Runner,
    deps_lock_entry: DepsLockEntry | None = None,
) -> DoctorCheck:
    # Not `shutil.which` any more: since the setup panel can install these
    # into the program's own tools directory, PATH is no longer the whole
    # question, and a doctor that could not see a copy the user had just
    # installed here would report a machine as broken one second after
    # fixing it. `toolchain` owns the order.
    from mfp import toolchain

    executable = configured_path or toolchain.resolve(name)
    if not executable:
        return DoctorCheck(
            name=name,
            found=False,
            path=None,
            version=None,
            ok=False,
            error_code="dependency_missing",
            detail=f"{name} not found on PATH and no override configured",
            version_status=_version_status(None, deps_lock_entry),
        )

    try:
        result = runner([executable, *version_argv_tail])
    except (OSError, subprocess.SubprocessError) as exc:
        return DoctorCheck(
            name=name,
            found=True,
            path=executable,
            version=None,
            ok=False,
            error_code="dependency_missing",
            detail=f"{name} found at {executable} but failed to run: {exc}",
            version_status=_version_status(None, deps_lock_entry),
        )

    version = version_parser(result.stdout) or version_parser(result.stderr)
    if result.returncode != 0 or not version:
        return DoctorCheck(
            name=name,
            found=True,
            path=executable,
            version=version,
            ok=False,
            error_code="dependency_missing",
            detail=f"{name} at {executable} did not report a usable version",
            version_status=_version_status(version, deps_lock_entry),
        )

    status = _version_status(version, deps_lock_entry)
    below_minimum = status == "below_minimum" and deps_lock_entry is not None
    return DoctorCheck(
        name=name,
        found=True,
        path=executable,
        version=version,
        # Deliberately still ok. The binary runs, and most of what the
        # product does with it keeps working -- an old yt-dlp broke YouTube
        # and nothing else. Failing the whole report here would turn
        # `doctor` red on a machine that fetches Instagram, Threads, X and
        # Bilibili perfectly well, which is how a report teaches its reader
        # to ignore it (R6-5). The consequence is carried by
        # `version_status`, which `capabilities.py` reads to block the
        # affected platform at input instead.
        ok=True,
        detail=(
            f"{name} {version} is older than the required minimum "
            f"{deps_lock_entry.min_version}; capabilities needing the newer "
            "version are refused at input rather than failing mid-transfer"
            if below_minimum
            else None
        ),
        version_status=status,
    )


#: Engines yt-dlp can run YouTube's player script in. `deno` first because
#: it is the only one yt-dlp enables without being asked; the others need
#: `--js-runtimes` and are listed so a machine that already has one is not
#: told to install a third.
JS_RUNTIMES: tuple[str, ...] = ("deno", "node", "bun")

JS_RUNTIME_CHECK = "javascript-runtime"


def check_js_runtime(runner: Runner) -> DoctorCheck:
    """Is there a JavaScript engine for yt-dlp to solve YouTube's player in?

    Not required, and deliberately not -- but the reason is narrower than it
    looks, and this docstring used to state the opposite. Corrected
    2026-08-19 after crossing the two variables that had only ever been
    tested apart:

      yt-dlp 2026.08.18 + no runtime at all -> full DASH ladder transfers
      yt-dlp 2026.07.04 + node reachable    -> HTTP 403 on every format

    So a runtime is NOT what decides whether YouTube works; the yt-dlp
    version is (see `capabilities.py`, and `deps.lock.json`'s floor). What a
    runtime actually buys is the progressive single-file formats, which the
    fallback player client does not offer. The product muxes DASH anyway,
    so on most machines this check being red costs nothing at all -- which
    is exactly why it must not be worded as though YouTube depends on it.

    One trap this check cannot see, and says so: yt-dlp auto-enables only
    **deno**. node and bun work, but only when passed `--js-runtimes`, so
    finding one on PATH is not the same as yt-dlp being able to use it.
    """
    for name in JS_RUNTIMES:
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            result = runner([executable, "--version"])
        except (OSError, subprocess.SubprocessError):
            continue
        version = _parse_first_line(result.stdout) or _parse_first_line(result.stderr)
        return DoctorCheck(
            name=JS_RUNTIME_CHECK,
            found=True,
            path=executable,
            version=version,
            ok=True,
            detail=(
                f"{name} found. This adds YouTube's progressive single-file "
                "formats; DASH video+audio transfers do not need it"
                + (
                    ""
                    if name == "deno"
                    else f". Note yt-dlp auto-enables only deno, so {name} is "
                    "not used unless --js-runtimes is passed"
                )
            ),
        )

    return DoctorCheck(
        name=JS_RUNTIME_CHECK,
        found=False,
        path=None,
        version=None,
        ok=False,
        error_code="dependency_missing",
        detail=(
            "no JavaScript engine found ("
            + ", ".join(JS_RUNTIMES)
            + "). Only YouTube's progressive single-file formats are affected; "
            "the DASH video+audio this product downloads works without one, so "
            "installing an engine is optional. Note that Java is a different "
            "thing and does not satisfy this"
        ),
    )


# --- Chrome detection (never executes chrome.exe) --------------------------

_CHROME_APP_PATHS_REGISTRY_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
)

_CHROME_COMMON_INSTALL_PATHS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)

_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+){2,3}$")


def find_chrome_executable(chrome_config: ChromeConfig) -> Path | None:
    """Locate chrome.exe without ever running it."""
    if chrome_config.executable_path:
        candidate = Path(chrome_config.executable_path)
        if candidate.is_file():
            return candidate

    for hive, subkey in _CHROME_APP_PATHS_REGISTRY_KEYS:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "")
                candidate = Path(value)
                if candidate.is_file():
                    return candidate
        except OSError:
            continue

    for template in _CHROME_COMMON_INSTALL_PATHS:
        candidate = Path(os.path.expandvars(template))
        if candidate.is_file():
            return candidate

    return None


def get_chrome_version(executable: Path) -> str | None:
    """Determine Chrome's version without launching it.

    Primary source: the versioned subdirectory Chrome installs next to
    chrome.exe (e.g. `Application\\121.0.6167.85\\`). Fallback: the
    `HKCU\\Software\\Google\\Chrome\\BLBeacon\\version` registry value,
    which Chrome itself writes after having been run at least once.
    """
    app_dir = executable.parent
    try:
        version_dirs = [
            entry.name
            for entry in app_dir.iterdir()
            if entry.is_dir() and _VERSION_DIR_RE.match(entry.name)
        ]
    except OSError:
        version_dirs = []

    if version_dirs:
        def _key(v: str) -> tuple[int, ...]:
            return tuple(int(p) for p in v.split("."))

        return max(version_dirs, key=_key)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
            value, _ = winreg.QueryValueEx(key, "version")
            return str(value)
    except OSError:
        return None


def default_chrome_profile_dir() -> Path:
    """Chrome's default (unconfigured) user-data directory on Windows."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "Google" / "Chrome" / "User Data"


def resolve_chrome_profile_dir(chrome_config: ChromeConfig) -> Path:
    """Resolve the configured profile dir, applying the `null` default
    from PSM §4.6 ("null = <appdata>/chrome-profile")."""
    if chrome_config.profile_dir:
        return Path(chrome_config.profile_dir)
    return app_data_dir() / "chrome-profile"


def is_default_chrome_profile_dir(path: Path) -> bool:
    """True iff `path` resolves to Chrome's own default user-data dir.

    Equality, not containment, because equality is exactly what triggers the
    Chrome 136 behaviour this predicate is named after. Containment is a
    different problem with a different answer -- see below.
    """
    try:
        real_candidate = os.path.normcase(os.path.realpath(path))
        real_default = os.path.normcase(os.path.realpath(default_chrome_profile_dir()))
    except OSError:
        return False
    return real_candidate == real_default


def is_inside_default_chrome_profile_dir(path: Path) -> bool:
    """True when `path` sits INSIDE Chrome's default user-data directory.

    A separate rule from the one above and refused for a separate reason.
    `...\\User Data\\Default` is the folder people actually see when they go
    looking for their profile, and it is not the default *user-data dir*, so
    Chrome would honour the debugging switches and start normally -- while
    creating a nested profile tree inside the user's real Chrome data. Writing
    into somebody's live browser profile to fetch a photo is not a trade this
    tool gets to make on their behalf.

    False for the directory itself; `is_default_chrome_profile_dir` owns that
    case and says something more specific about it.
    """
    try:
        real_candidate = Path(os.path.normcase(os.path.realpath(path)))
        real_default = Path(os.path.normcase(os.path.realpath(default_chrome_profile_dir())))
    except OSError:
        return False
    return real_candidate != real_default and real_default in real_candidate.parents


def check_chrome(chrome_config: ChromeConfig) -> DoctorCheck:
    profile_dir = resolve_chrome_profile_dir(chrome_config)
    if is_default_chrome_profile_dir(profile_dir):
        return DoctorCheck(
            name="chrome",
            found=True,
            path=None,
            version=None,
            ok=False,
            error_code=ChromeDefaultProfileError.error_code,
            detail=(
                "Configured Chrome profile directory resolves to Chrome's "
                "default user data directory. Chrome 136+ ignores remote "
                "debugging switches in this case (PSM §5.2); configure a "
                "dedicated, non-default profileDir."
            ),
        )

    if is_inside_default_chrome_profile_dir(profile_dir):
        # doctor and capture must agree: reporting this as OK and then having
        # `capture` refuse it would send the user hunting in the wrong place.
        return DoctorCheck(
            name="chrome",
            found=True,
            path=None,
            version=None,
            ok=False,
            error_code=ChromeDefaultProfileError.error_code,
            detail=(
                "Configured Chrome profile directory is inside Chrome's real "
                "user data directory. Launching there would write a nested "
                "profile into your live browser data; configure a dedicated "
                "directory outside it."
            ),
        )

    executable = find_chrome_executable(chrome_config)
    if not executable:
        return DoctorCheck(
            name="chrome",
            found=False,
            path=None,
            version=None,
            ok=False,
            error_code="dependency_missing",
            detail="Chrome executable not found (checked configured path, registry, common install paths)",
        )

    version = get_chrome_version(executable)
    return DoctorCheck(
        name="chrome",
        found=True,
        path=str(executable),
        version=version,
        ok=True,
        detail=None if version else "Chrome found but version could not be determined without launching it",
    )


def run_doctor(
    config: AppConfig,
    *,
    runner: Runner | None = None,
    deps_lock_path: Path | None = None,
) -> DoctorReport:
    """Run every dependency check and produce a DoctorReport.

    `runner` is injected for subprocess-invoking checks (yt-dlp, ffmpeg, the
    JavaScript runtime, the recognition engine) so tests never spawn real
    processes; Chrome detection never uses a subprocess at all (see module
    docstring).
    `deps_lock_path` is injected so tests control which deps.lock.json (if
    any) is read; a missing/invalid lock file degrades to `version_status:
    null` on every check rather than failing `doctor`.
    """
    active_runner = runner or _default_runner
    deps_lock = load_deps_lock(deps_lock_path)

    checks = [
        _check_subprocess_binary(
            "yt-dlp",
            configured_path=config.binaries.yt_dlp,
            version_argv_tail=["--version"],
            version_parser=_parse_first_line,
            runner=active_runner,
            deps_lock_entry=deps_lock.yt_dlp if deps_lock else None,
        ),
        # gallery-dl was checked here until 2026-08-30. See REQUIRED_BINARIES
        # for what it was for, why nothing calls it, and the three conditions
        # that would bring it back. `deps.lock.json` keeps its entry: removing
        # a field is a schema change, and the lock file is read by builds.
        _check_subprocess_binary(
            "ffmpeg",
            configured_path=config.binaries.ffmpeg,
            version_argv_tail=["-version"],
            version_parser=_parse_ffmpeg_version,
            runner=active_runner,
            deps_lock_entry=deps_lock.ffmpeg if deps_lock else None,
        ),
        check_js_runtime(active_runner),
        check_chrome(config.chrome),
    ]

    for check in checks:
        check.required = check.name in REQUIRED_BINARIES

    overall_ok = all(check.ok for check in checks if check.required)
    return DoctorReport(ok=overall_ok, checks=checks, exit_code=0 if overall_ok else 6)
