"""Chrome launch and CDP session for the Instagram adapter (PSM §5.2–5.3).

This module is the moat. Everything about it is measured rather than
preferred, and three of its rules are the kind that fail silently if broken:

- **`--headless` is prohibited, any variant.** Headless is the fingerprint
  Meta blocks (Phase 1 §N4). Not a preference; the whole acquisition premise
  is that this is the user's real Chrome behaving like a real browser.
- **`--user-data-dir` must not be Chrome's default profile.** Since Chrome
  136 the debugging switches are ignored *entirely* when it is, so the launch
  appears to work and the CDP connect then times out for no visible reason
  (Phase 1 §H5). We refuse up front instead, naming the constraint.
- **The port is discovered, never assumed.** `--remote-debugging-port=0` makes
  Chrome pick a free port and write it to `DevToolsActivePort`; a hard-coded
  port collides with the user's own Chrome and satisfies nothing.

Everything with a network or process boundary is injected, so the tests here
never launch a browser and never open a socket. What that buys is real: the
lifecycle rules below (no orphan process, ever) are exactly the ones that are
painful to test against a live browser and easy to get wrong.
"""

from __future__ import annotations

import json
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from mfp.config import ChromeConfig
from mfp.doctor import (
    find_chrome_executable,
    is_default_chrome_profile_dir,
    is_inside_default_chrome_profile_dir,
    resolve_chrome_profile_dir,
)
from mfp.errors import (
    CdpTimeoutError,
    ChromeDefaultProfileError,
    DependencyMissingError,
)

#: Seconds to wait for Chrome to write `DevToolsActivePort` (PSM §5.2).
PORT_DISCOVERY_TIMEOUT_S = 15.0

#: Seconds to wait for `Page.loadEventFired` (PSM §5.3 step 5).
PAGE_LOAD_TIMEOUT_S = 30.0

#: Seconds to wait for any single CDP command to answer.
COMMAND_TIMEOUT_S = 10.0

#: Written next to the queue so an abnormal exit can be reaped next start.
PID_FILE_NAME = "chrome.pid"

#: Rejected before launch. Chrome accepts several spellings and each one
#: silently defeats the entire acquisition strategy, so the check is on the
#: string prefix rather than an exact match.
PROHIBITED_FLAG_PREFIX = "--headless"


class ChromeProcess(Protocol):
    """The part of `subprocess.Popen` this module uses."""

    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = ...) -> int: ...


#: Injected so tests never spawn a browser.
Launcher = Callable[[list[str]], ChromeProcess]


class CdpConnection(Protocol):
    """A connected CDP websocket, reduced to what §5.3 needs."""

    def send(self, method: str, params: dict[str, Any] | None = None,
             session_id: str | None = None, timeout: float = ...) -> dict[str, Any]: ...

    def wait_for_event(self, method: str, timeout: float = ...) -> dict[str, Any]: ...

    def close(self) -> None: ...


#: Injected so tests never open a socket. Takes the browser websocket URL.
Connector = Callable[[str], CdpConnection]


def build_argv(executable: Path, profile_dir: Path, chrome: ChromeConfig) -> list[str]:
    """The normative flag list from PSM §5.2, in that order.

    `--remote-debugging-port=0` is first because it is the one flag whose
    absence turns every later failure into a mystery.
    """
    x, y = chrome.window_position
    return [
        str(executable),
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-extensions",
        f"--window-position={x},{y}",
        "--window-size=1280,900",
    ]


def assert_no_prohibited_flags(argv: list[str]) -> None:
    """Guard against `--headless` reaching Chrome by any route.

    It is checked on the built argv rather than trusted from `build_argv`
    because config, environment and future callers all get a say in what runs.
    """
    offenders = [flag for flag in argv if flag.startswith(PROHIBITED_FLAG_PREFIX)]
    if offenders:
        raise ChromeDefaultProfileError(
            f"refusing to launch Chrome with {offenders!r}: headless is the "
            "fingerprint this adapter exists to avoid (PSM §5.2). Remove it."
        )


def resolve_profile_dir(chrome: ChromeConfig) -> Path:
    """Resolve, then refuse the default profile (Chrome 136+, PSM §5.2)."""
    profile_dir = resolve_chrome_profile_dir(chrome)
    if is_default_chrome_profile_dir(profile_dir):
        raise ChromeDefaultProfileError(
            f"chrome.profileDir resolves to Chrome's default user data directory "
            f"({profile_dir}). Chrome 136 and later ignore the remote-debugging "
            "switches entirely for the default profile, so the launch would "
            "appear to succeed and every CDP call would then time out. "
            "Point chrome.profileDir at a dedicated directory."
        )
    if is_inside_default_chrome_profile_dir(profile_dir):
        # Not the Chrome 136 case -- this one would launch and work. It is
        # refused because `...\User Data\Default` is the path people naturally
        # reach for, and launching there builds a nested profile inside their
        # live browser data (measured 2026-08-16: the equality check let it
        # straight through).
        raise ChromeDefaultProfileError(
            f"chrome.profileDir ({profile_dir}) is inside Chrome's real user "
            "data directory. Launching there would create a nested profile in "
            "your live browser data. Point chrome.profileDir at a dedicated "
            "directory outside it -- leaving it null uses "
            "%APPDATA%\\media-fetch-pipeline\\chrome-profile."
        )
    return profile_dir


#: Chrome writes the port it chose here, and deletes the file on a *graceful*
#: shutdown. We do not shut it down gracefully -- `terminate()` is
#: TerminateProcess on Windows -- so the file survives every run.
DEVTOOLS_PORT_FILE_NAME = "DevToolsActivePort"


def clear_devtools_port(profile_dir: Path) -> None:
    """Delete a leftover `DevToolsActivePort` before launching.

    Without this, the SECOND `mfp capture` run against a profile fails and the
    first one succeeds -- measured 2026-08-16, and the error names a port
    nothing is listening on: `could not reach Chrome at
    http://127.0.0.1:59468/json/version: [WinError 10061]`. The mechanism is
    that the file outlives the browser (we kill it, so it never cleans up),
    `read_devtools_port` finds it on its first poll and returns the DEAD port
    without ever waiting for the new one.

    Deleting up front is what makes the poll below mean what it says: the file
    can then only have been written by the browser we just started. It also
    turns "Chrome is already running with this profile" into the timeout that
    already explains itself, instead of a connection refused on a stale port.
    """
    try:
        (profile_dir / DEVTOOLS_PORT_FILE_NAME).unlink(missing_ok=True)
    except OSError:
        # A locked file means a live Chrome owns this profile. The port poll
        # will then time out with the message that says exactly that, which is
        # a better outcome than refusing to start.
        pass


def read_devtools_port(
    profile_dir: Path,
    *,
    timeout_s: float = PORT_DISCOVERY_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll `<profile>/DevToolsActivePort` for the port Chrome chose.

    The file's first line is the port. It is written after the listener is
    up, which is what makes polling it correct rather than merely convenient
    -- there is no earlier moment at which connecting would work.

    Correct only because `clear_devtools_port` ran first: this function cannot
    tell a fresh file from a stale one, and a stale one reads as an instant
    success pointing at a dead port.
    """
    path = profile_dir / "DevToolsActivePort"
    deadline = clock() + timeout_s
    while True:
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
            if first_line:
                return int(first_line)
        except (OSError, IndexError, ValueError):
            pass  # not written yet, or half-written
        if clock() >= deadline:
            raise CdpTimeoutError(
                f"Chrome did not report a debugging port within {timeout_s:g}s "
                f"({path} absent or empty). If Chrome is already running with "
                "this profile, close it first."
            )
        sleep(0.1)


# --- orphan reaping (PSM §5.2 lifecycle) -------------------------------------


def write_pid(pid_path: Path, pid: int) -> None:
    """Record the PID so an abnormal exit can be cleaned up next start."""
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass  # best effort: failing to record must not stop the run


def clear_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def reap_orphan(
    pid_path: Path,
    *,
    killer: Callable[[int], None] | None = None,
) -> int | None:
    """Kill a Chrome left behind by an abnormal exit. Returns the PID killed.

    Deliberately best-effort and deliberately quiet: the recorded PID may
    have been reused by an unrelated process by now, so a failure to kill is
    the *expected* outcome in the common case and must not be an error. What
    must not happen is the PID file surviving to be retried forever.
    """
    try:
        recorded = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None

    clear_pid(pid_path)
    try:
        (killer or _default_killer)(recorded)
        return recorded
    except (OSError, ProcessLookupError, PermissionError):
        return None


def _default_killer(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def terminate(process: ChromeProcess, *, grace_s: float = 5.0) -> None:
    """Ask, then insist. A browser left resident is a defect (PSM §5.2)."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=grace_s)
    except Exception:  # noqa: BLE001 -- any failure escalates to kill
        try:
            process.kill()
        except Exception:  # noqa: BLE001 -- nothing left to try
            pass


# --- session -----------------------------------------------------------------


@dataclass
class ChromeSession:
    """A launched browser plus its CDP connection.

    Only `fetch_html` is public surface for adapters; the rest is lifecycle.
    """

    connection: CdpConnection
    port: int
    process: ChromeProcess
    pid_path: Path
    page_load_timeout_s: float = PAGE_LOAD_TIMEOUT_S
    command_timeout_s: float = COMMAND_TIMEOUT_S
    _targets: list[str] = field(default_factory=list)

    def fetch_html(self, url: str) -> str:
        """Navigate to `url` and return `document.documentElement.outerHTML`.

        Waits for `Page.loadEventFired` and nothing more. Not network-idle,
        not a render: spike-01 measured the payload to be present in the
        script tags at load, with zero `<video>` elements, so waiting longer
        buys nothing and costs seconds per post.
        """
        target = self._create_target()
        try:
            session_id = self._attach(target)
            self._send("Page.enable", session_id=session_id)
            self._send("Page.navigate", {"url": url}, session_id=session_id)
            self._wait_for_load()
            result = self._send(
                "Runtime.evaluate",
                {
                    "expression": "document.documentElement.outerHTML",
                    "returnByValue": True,
                },
                session_id=session_id,
            )
            return _unwrap_evaluate(result)
        finally:
            self._close_target(target)

    # -- internals ------------------------------------------------------------

    def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.connection.send(
                method, params, session_id=session_id, timeout=self.command_timeout_s
            )
        except CdpTimeoutError:
            raise
        except TimeoutError as exc:
            raise CdpTimeoutError(f"CDP {method} timed out") from exc

    def _wait_for_load(self) -> None:
        try:
            self.connection.wait_for_event(
                "Page.loadEventFired", timeout=self.page_load_timeout_s
            )
        except CdpTimeoutError:
            raise
        except TimeoutError as exc:
            raise CdpTimeoutError(
                f"page did not finish loading within {self.page_load_timeout_s:g}s"
            ) from exc

    def _create_target(self) -> str:
        result = self._send("Target.createTarget", {"url": "about:blank"})
        target_id = result.get("targetId")
        if not target_id:
            raise CdpTimeoutError("Target.createTarget returned no targetId")
        self._targets.append(target_id)
        return target_id

    def _attach(self, target_id: str) -> str:
        result = self._send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise CdpTimeoutError("Target.attachToTarget returned no sessionId")
        return session_id

    def _close_target(self, target_id: str) -> None:
        # A tab left open is not fatal but it accumulates across a batch, so
        # this is best-effort rather than conditional on the fetch succeeding.
        try:
            self._send("Target.closeTarget", {"targetId": target_id})
        except Exception:  # noqa: BLE001
            pass
        if target_id in self._targets:
            self._targets.remove(target_id)

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001
            pass
        terminate(self.process)
        clear_pid(self.pid_path)


def _unwrap_evaluate(result: dict[str, Any]) -> str:
    """Pull the string out of a `Runtime.evaluate` reply.

    A CDP exception comes back as a normal reply with `exceptionDetails`, not
    as an error, so an unchecked read here would return "None" as HTML.
    """
    if "exceptionDetails" in result:
        raise CdpTimeoutError(f"page evaluation failed: {result['exceptionDetails']}")
    value = result.get("result", {}).get("value")
    if not isinstance(value, str):
        raise CdpTimeoutError(
            f"page evaluation returned {type(value).__name__}, expected a string"
        )
    return value


def browser_websocket_url(port: int, *, http_get: Callable[[str], str]) -> str:
    """`GET /json/version` -> `webSocketDebuggerUrl` (PSM §5.3 step 1)."""
    raw = http_get(f"http://127.0.0.1:{port}/json/version")
    try:
        url = json.loads(raw)["webSocketDebuggerUrl"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CdpTimeoutError(
            f"Chrome's /json/version gave no webSocketDebuggerUrl: {raw[:200]!r}"
        ) from exc
    return str(url)


@contextmanager
def chrome_session(
    chrome: ChromeConfig,
    *,
    state_dir: Path,
    launcher: Launcher,
    connector: Connector,
    http_get: Callable[[str], str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    port_timeout_s: float = PORT_DISCOVERY_TIMEOUT_S,
) -> Iterator[ChromeSession]:
    """Launch Chrome, connect CDP, and guarantee both are gone afterwards.

    The `finally` is the point of the whole function: M2's acceptance says
    zero orphan Chrome processes after a run, and every early return between
    launch and connect is a chance to leave one behind.
    """
    executable = find_chrome_executable(chrome)
    if executable is None:
        raise DependencyMissingError(
            "Chrome executable not found (checked configured path, registry and "
            "common install paths). `mfp doctor` reports where it looked."
        )

    profile_dir = resolve_profile_dir(chrome)
    profile_dir.mkdir(parents=True, exist_ok=True)

    argv = build_argv(executable, profile_dir, chrome)
    assert_no_prohibited_flags(argv)

    pid_path = state_dir / PID_FILE_NAME
    reap_orphan(pid_path)

    # Before the launch, not after: the poll below cannot distinguish this
    # run's port file from the last run's.
    clear_devtools_port(profile_dir)

    process = launcher(argv)
    write_pid(pid_path, process.pid)

    session: ChromeSession | None = None
    try:
        port = read_devtools_port(
            profile_dir, timeout_s=port_timeout_s, clock=clock, sleep=sleep
        )
        connection = connector(browser_websocket_url(port, http_get=http_get))
        session = ChromeSession(
            connection=connection, port=port, process=process, pid_path=pid_path
        )
        yield session
    finally:
        if session is not None:
            session.close()
        else:
            # Failed before the session existed -- the browser is still ours.
            terminate(process)
            clear_pid(pid_path)


__all__ = [
    "COMMAND_TIMEOUT_S",
    "DEVTOOLS_PORT_FILE_NAME",
    "PAGE_LOAD_TIMEOUT_S",
    "PID_FILE_NAME",
    "PORT_DISCOVERY_TIMEOUT_S",
    "CdpConnection",
    "ChromeProcess",
    "ChromeSession",
    "assert_no_prohibited_flags",
    "browser_websocket_url",
    "build_argv",
    "chrome_session",
    "clear_devtools_port",
    "clear_pid",
    "read_devtools_port",
    "reap_orphan",
    "resolve_profile_dir",
    "terminate",
    "write_pid",
]
