"""Put the install directory on the user's PATH, and take it back off.

This is the *discovery* half of O-9, and the only half that is not tied to
one vendor: a skills directory helps Claude Code, an `AGENTS.md` helps
whatever reads that repo, but "the command exists and `where mfp` finds it"
is how every agent that can run a shell finds anything at all.

It lives in the product rather than in NSIS for three reasons, and the third
is the one that decided it:

  1. the installer already ships `mfp.exe`, so the installer calling it is
     one moving part instead of two;
  2. NSIS's `ReadRegStr` is bounded by the build's `NSIS_MAX_STRLEN`, and a
     PATH longer than that would come back truncated -- writing that back is
     how an installer eats somebody's PATH;
  3. the string handling is the part that can go wrong, and here it is a
     pure function with tests. `HKCU\\Environment` is touched by exactly two
     functions below, both of which do nothing but read and write.

Only ever the PER-USER PATH (`HKCU\\Environment`). The machine-wide one under
HKLM is not ours to edit -- this is a per-user install (`perMachine: false`)
and needs no elevation, which is also why nothing here can ask for any.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from mfp.errors import MfpError

#: `HKCU\Environment` -- the per-user block. Not `SYSTEM\...\Session Manager\
#: Environment`, which is the machine-wide one.
ENVIRONMENT_KEY = "Environment"
PATH_VALUE = "Path"


def entries(value: str) -> list[str]:
    """Split a PATH value, dropping the empty segments Windows tolerates."""
    return [part for part in value.split(";") if part.strip()]


def _same(a: str, b: str) -> bool:
    """Windows paths compare case-insensitively, and a trailing separator is
    not a difference. Getting this wrong appends a second copy every run."""
    return a.strip().rstrip("\\/").casefold() == b.strip().rstrip("\\/").casefold()


def add_entry(value: str, directory: str) -> str:
    """Return the PATH with `directory` present exactly once, appended.

    Appended rather than prepended on purpose: this tool has no business
    shadowing a command the user already has by that name.
    """
    existing = entries(value)
    if any(_same(part, directory) for part in existing):
        return value
    return ";".join([*existing, directory])


def remove_entry(value: str, directory: str) -> str:
    """Return the PATH with every copy of `directory` gone, order kept."""
    return ";".join(part for part in entries(value) if not _same(part, directory))


def contains(value: str, directory: str) -> bool:
    return any(_same(part, directory) for part in entries(value))


def default_directory() -> Path:
    """The directory an agent would need on PATH to reach this build.

    One expression covers both cases and that is not a coincidence: frozen,
    `sys.executable` IS `mfp.exe`; in a checkout it is the interpreter in
    `.venv/Scripts`, which is exactly where `pip install -e .` put the
    `mfp` console script. Deriving it from this file's location instead
    would give the source tree, where no executable lives.
    """
    return Path(sys.executable).parent


def _require_windows() -> None:
    if sys.platform != "win32":
        raise MfpError("the user PATH is a Windows registry value; nothing to edit here")


def read_user_path() -> tuple[str, int]:
    """The raw per-user PATH and its registry type.

    Raw: `winreg` does not expand `REG_EXPAND_SZ`, which is what we want --
    expanding it and writing the result back would bake today's `%USERPROFILE%`
    into somebody's PATH forever.
    """
    _require_windows()
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY) as key:
        try:
            value, kind = winreg.QueryValueEx(key, PATH_VALUE)
        except FileNotFoundError:
            # A user profile with no per-user PATH at all is unusual but
            # legal, and it is not an error -- it is an empty one.
            return "", winreg.REG_EXPAND_SZ
    return str(value), int(kind)


def write_user_path(value: str, kind: int) -> None:
    """Write the per-user PATH back, keeping the type it already had."""
    _require_windows()
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, PATH_VALUE, 0, kind, value)


def broadcast_change() -> bool:
    """Tell already-running programs the environment moved.

    Without this the new PATH reaches only processes started after the next
    logon, so the user ticks the box, opens their terminal, and the command
    is not there. Failure is reported, not raised: the PATH is written
    either way and a sign-out fixes it.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002

    result = ctypes.c_ulong()
    sent = ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        ctypes.c_wchar_p("Environment"),
        SMTO_ABORTIFHUNG,
        5000,
        ctypes.byref(result),
    )
    return bool(sent)


@dataclass(frozen=True)
class PathPlan:
    directory: str
    action: str  # added | already-present | removed | absent
    entry_count: int
    broadcast: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": self.directory,
            "action": self.action,
            "entryCount": self.entry_count,
            "broadcast": self.broadcast,
        }


def apply_path(directory: Path, *, remove: bool = False, dry_run: bool = False) -> PathPlan:
    """Add or remove one directory on the per-user PATH.

    Idempotent in both directions, and a no-op writes nothing at all -- the
    registry is not touched when the answer is already correct, so a second
    install cannot corrupt a PATH it has no reason to rewrite.
    """
    target = str(directory)
    current, kind = read_user_path()
    present = contains(current, target)

    if remove:
        if not present:
            return PathPlan(target, "absent", len(entries(current)), False)
        updated, action = remove_entry(current, target), "removed"
    else:
        if present:
            return PathPlan(target, "already-present", len(entries(current)), False)
        updated, action = add_entry(current, target), "added"

    if dry_run:
        return PathPlan(target, action, len(entries(updated)), False)

    write_user_path(updated, kind)
    return PathPlan(target, action, len(entries(updated)), broadcast_change())
