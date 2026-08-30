"""`mfp serve` runtime wiring (PSM Batch 2 §4.1; G6 §5.1, §7).

`server/app.py` is a pure factory that takes its dependencies as arguments so
tests can build it against a temp dir. This module is the other half: it
resolves the *real* paths, refuses to bind anywhere O-3 prohibits, guards the
queue against a second writer, and hands the app to uvicorn.

Kept separate from `server/app.py` on purpose -- importing uvicorn is not free,
and no test of the app factory should have to pay for it.

G6 adds four opt-in behaviours the Electron shell needs. A bare `mfp serve`
still behaves exactly as it did before them:

  ``--port 0``              bind an OS-assigned port and report which one
  ``--ready-json``          print one machine-readable line once listening
  ``--exit-on-stdin-eof``   exit when the parent process dies
  ``--gui-dist <dir>``      serve the built SPA at ``/``
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mfp.config import AppConfig, app_data_dir, load_config, save_config
from mfp.errors import QueueLocked

# `mfp.server.app` and `mfp.server.security` both pull in FastAPI, ~400 ms of
# import on this machine. They are deferred to the functions that need them
# for the same reason uvicorn is: `build_parser()` imports this module so that
# `mfp serve` and `mfp-sidecar` cannot offer different flags, and that made
# every other verb -- `mfp doctor` above all -- pay for a web framework it
# never touches.

_QUEUE_FILE_NAME = "queue.json"
_QUEUE_LOCK_FILE_NAME = "queue.lock"

#: Carried in the ready line so the shell can refuse a sidecar whose contract
#: it does not understand, instead of failing later in an unrelated place.
READY_API_VERSION = 1


def default_queue_path() -> Path:
    """`%APPDATA%/media-fetch-pipeline/queue.json` (PSM Batch 2 §4.5)."""
    return app_data_dir() / _QUEUE_FILE_NAME


def default_queue_lock_path() -> Path:
    """The single-writer lock, beside the queue it protects (G6 §5.1).

    Derived from `default_queue_path()` rather than from `app_data_dir()` so
    that redirecting the queue -- in a test or a future `--queue-path` --
    moves the lock with it. A lock guarding a different file than the one
    being written is worse than no lock at all.
    """
    return default_queue_path().with_name(_QUEUE_LOCK_FILE_NAME)


class NonLoopbackBind(Exception):
    """Raised when `serve` is asked to bind a non-loopback address.

    This is a hard stop, not a warning. The API has no authentication by
    design (O-3): the loopback guard rejects cross-site *browser* traffic,
    but nothing stops another machine on the LAN from calling a
    0.0.0.0-bound port directly. Exposing it needs real auth, which does
    not exist yet -- so the bind is refused rather than mitigated.
    """


# --- single-queue-writer lock (G6 §5.1) -------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


class _FILETIME(ctypes.Structure):
    """The Win32 64-bit timestamp, split across two 32-bit halves."""

    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]


#: FILETIME's zero. Kept as an aware UTC datetime so every value derived
#: from it compares directly against what `_create()` writes into the lock.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

#: How far a process's creation time may sit *after* the lock's `startedAt`
#: and still be believed to be the holder.
#:
#: Only used for locks written before `processStartedAt` existed, where the
#: identity has to be inferred from an inequality: the holder was created
#: BEFORE it wrote its own lock, so a process created after that instant
#: cannot be it. The margin exists so nothing but a real recycle can ever
#: be read as one -- steal too eagerly here and two writers share a queue,
#: which is the failure this whole lock exists to prevent.
_LOCK_IDENTITY_MARGIN_S = 1.0

#: How many times `acquire()` re-reads a lock file it cannot make sense of
#: before ruling on it, and how long it waits between looks. `_create()`
#: publishes its payload atomically, so the only readers that need this are
#: ones racing the no-hard-links fallback or a scanner holding the file open
#: for a moment -- both measured in milliseconds.
_LOCK_READ_ATTEMPTS = 5
_LOCK_READ_DELAY_S = 0.02

#: An unreadable lock file younger than this is assumed to be a create still
#: in flight and is refused, never reclaimed; older than that, nothing can
#: still be writing it, so it is garbage and gets reclaimed. Generous on
#: purpose -- being wrong upwards costs one retry, being wrong downwards puts
#: two writers on one queue.
_LOCK_MIDWRITE_GRACE_S = 5.0

#: What one look at the lock file can find. The split that matters is
#: `_LOCK_EMPTY` vs `_LOCK_CORRUPT`: both used to read as "names nobody,
#: therefore stale", and the first one is a live holder mid-create.
#:
#: `_LOCK_EMPTY` and `_LOCK_UNOPENABLE` are judged differently on purpose.
#: An empty file makes a claim about its content, so its age can settle
#: whether anything is still writing it. An unopenable one makes no claim at
#: all -- it may be a perfectly good lock held by a live process that a
#: scanner has open for a moment -- so age proves nothing and it is never
#: reclaimed.
_LOCK_ABSENT = "absent"
_LOCK_EMPTY = "empty"
_LOCK_UNOPENABLE = "unopenable"
_LOCK_CORRUPT = "corrupt"
_LOCK_HELD = "held"


def process_is_alive(pid: int) -> bool:
    """True when a process with `pid` is currently running.

    **`os.kill(pid, 0)` is not usable here.** On Windows, CPython maps
    `os.kill` to `TerminateProcess` for every signal other than
    `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`, so the conventional POSIX liveness
    probe would kill the very process it is asking about. This uses
    `OpenProcess` + `GetExitCodeProcess` instead, which only reads.

    A handle we are not permitted to open (`ERROR_ACCESS_DENIED`) counts as
    *alive*: the safe answer when the question cannot be answered is "do not
    steal the lock". In practice this never fires for a real holder --
    `%APPDATA%` is per-user, so the holder always belongs to the same user
    as the process asking.
    """
    if pid <= 0:
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED

    try:
        # Seeded with STILL_ACTIVE on purpose: a failed GetExitCodeProcess
        # leaves the buffer untouched, so an unanswerable query reports
        # "alive" -- the same conservative default as ACCESS_DENIED above,
        # expressed without a branch that no test could ever reach.
        exit_code = ctypes.c_ulong(_STILL_ACTIVE)
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(exit_code))
        # A process object outlives the process itself while any handle to it
        # is open, so "OpenProcess succeeded" is not the same as "running".
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_started_at(pid: int) -> datetime | None:
    """When the process currently holding `pid` was created, or None.

    **A PID is not an identity.** Windows recycles process ids aggressively,
    and `process_is_alive()` can only answer "is *a* process wearing this
    number", never "is it *the* one". Measured on this machine: a sidecar
    killed at 03:04:56 left a lock naming PID 35532, and the next day at
    03:04:48 the OS handed 35532 to `dllhost.exe`. The lock then named a
    live process forever, and the app refused to start with no way out but
    deleting a file the user was never told about.

    The creation time is what makes the id an identity: two processes can
    share a number, but not a number *and* the instant it was issued.

    None means the question could not be answered -- no handle, or
    `GetProcessTimes` refused -- and every caller must read that as "assume
    it IS the holder", the same conservative default as `process_is_alive`.
    """
    if pid <= 0:
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None

    try:
        creation = _FILETIME()
        unused = (_FILETIME(), _FILETIME(), _FILETIME())
        ok = kernel32.GetProcessTimes(
            ctypes.c_void_p(handle),
            ctypes.byref(creation),
            *(ctypes.byref(slot) for slot in unused),
        )
        if not ok:
            return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    if ticks <= 0:
        return None
    # FILETIME counts 100-nanosecond intervals from 1601-01-01 UTC. The
    # division to microseconds is exact and deterministic, so the same
    # process always renders to the same instant -- which is what lets the
    # comparison below be equality rather than a tolerance.
    return _FILETIME_EPOCH + timedelta(microseconds=ticks // 10)


def own_process_started_at() -> datetime | None:
    """This process's creation time, for the lock payload to carry."""
    return process_started_at(os.getpid())


def _parse_instant(raw: object) -> datetime | None:
    """One ISO timestamp out of a lock file, or None if it says nothing.

    A lock file is data from another process that may be older, newer or
    damaged, so every field is optional and no shape here may raise --
    an unreadable timestamp has to degrade to "cannot tell", which the
    caller already treats as "do not steal the lock".
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive timestamp is one we wrote before this field was always aware,
    # or one somebody edited. Reading it as UTC matches what we write.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class QueueLock:
    """Exclusive `queue.lock` beside `queue.json` (G6 §5.1).

    `O_CREAT | O_EXCL` decides *who wins*: the create either succeeds or
    fails, with no window between checking and creating. What it does not
    decide is *what the loser sees*, and that was a bug. Creating the file
    and only then writing the PID into it leaves the name on disk, empty,
    for as long as the write takes. A second process that lost the create
    inside that window read zero bytes, concluded the lock named nobody,
    reclaimed it -- and both processes went on to serve the same queue.

    So the payload is published atomically too: `_create()` writes it to a
    staged file and hard-links that finished file into place, which fails
    the same way `O_EXCL` does when the name is taken. Where the filesystem
    has no hard links the create-then-write path is used instead, and the
    tolerant reader -- `_settle()` -- is what covers the window it reopens.

    The PID inside is not the lock -- it is the diagnostic that makes the
    refusal actionable ("PID 1234 has it") and the evidence that lets a
    *stale* lock, left by a process that was killed before it could clean
    up, be reclaimed instead of requiring the user to delete a file they
    were never told about.
    """

    def __init__(
        self,
        path: Path,
        *,
        is_alive=process_is_alive,
        started_at=process_started_at,
    ) -> None:
        self.path = path
        self._is_alive = is_alive
        self._started_at = started_at
        self._held = False

    def acquire(self) -> None:
        """Take the lock, reclaiming a stale one. Raises `QueueLocked`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create()
            return
        except FileExistsError:
            pass

        state, holder = self._settle()

        if state == _LOCK_HELD:
            holder_pid = holder["pid"]
            if self._holder_is_running(holder):
                raise QueueLocked(
                    f"another media-fetch-pipeline process (PID {holder_pid}) is already "
                    f"serving this queue; close it first",
                    pid=holder_pid,
                    lockPath=str(self.path),
                )
        elif state == _LOCK_EMPTY:
            # Present, empty, and far too young to be anything but a create
            # still in flight. This is the case that used to be read as
            # "names nobody, therefore stale" -- reclaiming it is exactly how
            # two writers happen. Refuse; the holder that is mid-create will
            # be readable, and nameable, on the next attempt.
            raise QueueLocked(
                f"another media-fetch-pipeline process is starting up and is claiming "
                f"this queue lock ({self.path}); try again in a moment",
                lockPath=str(self.path),
            )
        elif state == _LOCK_UNOPENABLE:
            # We cannot read it, so we cannot show it names nobody -- and a
            # lock you cannot prove is dead is one you must not take. This
            # is the one refusal the user may have to clear by hand, so it
            # says so rather than leaving them to guess.
            raise QueueLocked(
                f"the queue lock {self.path} exists but cannot be read, so it cannot be "
                f"shown to be stale; if you are certain nothing is serving this queue, "
                f"delete the file",
                lockPath=str(self.path),
            )

        # Reclaimable: the writer is gone (`_LOCK_HELD` naming a dead PID or
        # a PID the OS has since handed to somebody else),
        # the file says nothing a PID can be read out of (`_LOCK_CORRUPT` --
        # including one left empty long enough that nothing can still be
        # writing it), or the holder released it while we were looking
        # (`_LOCK_ABSENT`). None of those can be a live holder, and refusing
        # on them would wedge the app on a corrupt byte.
        try:
            # `missing_ok`: `_LOCK_ABSENT` means it is already gone, and a
            # holder shutting down at just the wrong moment must not turn
            # into "could not reclaim".
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise QueueLocked(
                f"could not reclaim stale queue lock {self.path}: {exc}",
                lockPath=str(self.path),
            ) from exc

        try:
            self._create()
        except FileExistsError as exc:
            # Another process reclaimed the same stale lock microseconds ago.
            # Losing the race is the correct outcome; retrying would be how
            # two writers both end up believing they won.
            raise QueueLocked(
                f"lost the race to reclaim the stale queue lock {self.path}",
                lockPath=str(self.path),
            ) from exc

    def _holder_is_running(self, holder: dict[str, object]) -> bool:
        """Is the process that WROTE this lock still running?

        Not the same question as `process_is_alive(holder["pid"])`, and the
        difference is a defect the user hit: a PID outlives the process that
        wore it, so a recycled number made the lock immortal (see
        `process_started_at`).

        Three answers, and only the first one may reclaim:

        * the number is now worn by a process that started AFTER the lock was
          written -- it cannot be the writer, so the lock is stale;
        * the number is worn by a process whose start matches -- the holder
          is live, refuse;
        * the question cannot be answered (no handle, no permission, a lock
          from a build that recorded no start time and no `startedAt`
          either) -- assume the holder is live and refuse. A lock you cannot
          prove is dead is one you must not take.
        """
        pid = holder.get("pid")
        if not isinstance(pid, int) or not self._is_alive(pid):
            return False

        observed = self._started_at(pid)
        if observed is None:
            return True

        recorded = _parse_instant(holder.get("processStartedAt"))
        if recorded is not None:
            # Both sides come from `GetProcessTimes` through the same
            # conversion, so the same process always renders identically.
            return observed == recorded

        # An older lock file, from before the payload carried a start time.
        # The writer existed before it wrote, so anything created after that
        # instant is somebody else -- the only inference available, and it
        # still catches the recycle that caused this.
        written = _parse_instant(holder.get("startedAt"))
        if written is None:
            return True
        return observed <= written + timedelta(seconds=_LOCK_IDENTITY_MARGIN_S)

    def _create(self) -> None:
        """Create the lock file *with its payload already in it*.

        The staged file is written, flushed and closed before the lock's own
        name exists anywhere, and `os.link` then publishes that finished
        file under the name in a single step -- raising `FileExistsError`,
        exactly as `O_EXCL` would, if someone else got there first. A reader
        can therefore never catch this lock half-written.
        """
        claim: dict[str, object] = {
            "pid": os.getpid(),
            "startedAt": datetime.now(timezone.utc).isoformat(),
        }
        # The PID's identity, not just its number. Omitted rather than
        # written null when it cannot be read, so a reader can tell "this
        # build did not record one" from "this one is unknown" -- and so a
        # lock written here stays readable by a build that predates it.
        own_start = own_process_started_at()
        if own_start is not None:
            claim["processStartedAt"] = own_start.isoformat()
        payload = json.dumps(claim).encode("utf-8")

        # Same directory, so the link can never be cross-volume; random
        # suffix, so a `.tmp` orphaned by an earlier kill cannot collide
        # with this one even after the OS recycles our PID.
        staged = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        )
        self._write_new(staged, payload)
        try:
            try:
                os.link(staged, self.path)
            except FileExistsError:
                raise  # the name is taken; `acquire()` knows what to do
            except OSError:
                # A filesystem without hard links (FAT/exFAT, some network
                # shares). Fall back to create-then-write rather than
                # refusing to start; `_settle()` is what keeps the window
                # this reopens from being misread as a stale lock.
                self._write_new(self.path, payload)
        finally:
            try:
                os.unlink(staged)
            except OSError:
                pass
        self._held = True

    @staticmethod
    def _write_new(path: Path, payload: bytes) -> None:
        """`O_EXCL`-create `path` and leave the payload flushed to disk."""
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _settle(self) -> tuple[str, dict[str, object] | None]:
        """Read the lock file, giving a create in flight time to land.

        An empty or unopenable file is the signature of a writer that has
        claimed the name and not yet published its payload, so it is re-read
        rather than believed. Only an *empty* one that outlasts the budget
        is then judged by its age -- nothing is mid-write seconds later, so
        an old one is garbage the caller may reclaim. An unopenable one is
        never reclaimed at any age: it has told us nothing to reclaim it on.
        """
        for attempt in range(_LOCK_READ_ATTEMPTS):
            state, holder = self._observe()
            if state not in (_LOCK_EMPTY, _LOCK_UNOPENABLE):
                return state, holder
            if attempt + 1 < _LOCK_READ_ATTEMPTS:
                time.sleep(_LOCK_READ_DELAY_S)
        if state == _LOCK_UNOPENABLE:
            return _LOCK_UNOPENABLE, None
        return (_LOCK_EMPTY if self._is_fresh() else _LOCK_CORRUPT), None

    def _observe(self) -> tuple[str, dict[str, object] | None]:
        """One look at the lock file, classified.

        A `_LOCK_CORRUPT` verdict still carries whatever parsed, if anything
        did: JSON without a usable PID is a file we can at least quote.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _LOCK_ABSENT, None
        except OSError:
            # Held open by something else for a moment -- a scanner, a
            # backup agent -- or unreadable for good. Worth the same
            # patience as a mid-write file, but not the same verdict.
            return _LOCK_UNOPENABLE, None

        if not raw.strip():
            return _LOCK_EMPTY, None

        try:
            holder = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _LOCK_CORRUPT, None

        if not isinstance(holder, dict):
            return _LOCK_CORRUPT, None
        if isinstance(holder.get("pid"), int):
            return _LOCK_HELD, holder
        return _LOCK_CORRUPT, holder

    def _is_fresh(self) -> bool:
        """True when the lock file was touched too recently to be garbage."""
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age < _LOCK_MIDWRITE_GRACE_S

    def read_holder(self) -> dict[str, object] | None:
        """Whatever the lock file claims, or None if it says nothing usable.

        A single look, deliberately: this is the diagnostic accessor. The
        decision procedure that has to tell "mid-write" apart from "names
        nobody" is `_settle()`, and only `acquire()` needs it.
        """
        return self._observe()[1]

    def release(self) -> None:
        """Drop the lock. Safe to call when it was never taken."""
        if not self._held:
            return
        self._held = False
        try:
            self.path.unlink()
        except OSError:
            # Someone removed it under us. There is nothing to protect any
            # more and nothing useful to say, and raising here would turn a
            # clean shutdown into a crash report.
            pass


# --- ready handshake and parent-death watch (G6 §4.1, §7.2, §7.3) -----------


def emit_ready(port: int, *, stream=None) -> None:
    """Print the one line the Electron shell waits for (G6 §4.1).

    Exactly one line, on stdout, and nothing else is ever written there --
    the shell parses stdout and treats anything unparseable as noise. All
    human/diagnostic output goes to stderr (PSM §4.1).
    """
    target = stream if stream is not None else sys.stdout
    target.write(
        json.dumps(
            {
                "event": "ready",
                "port": port,
                "apiVersion": READY_API_VERSION,
                "pid": os.getpid(),
            }
        )
        + "\n"
    )
    target.flush()


def _watch_stdin_eof(exit_fn=None, stdin=None) -> None:
    """Block on stdin; exit the process the moment it closes.

    `os._exit`, not `sys.exit`: the parent is already gone, there is nothing
    to unwind gracefully for, and a hung atexit handler would leave exactly
    the orphan process this mechanism exists to prevent. An abrupt exit is
    already safe -- every mutating endpoint persists before responding (G3)
    and INV-4 demotes PROBING/DOWNLOADING to PAUSED on load.
    """
    stream = stdin if stdin is not None else sys.stdin
    try:
        while True:
            if not stream.buffer.read(1):
                break
    except Exception:
        # A closed/detached stdin raises rather than returning b"" on some
        # paths. Either way the parent is unreachable, which is the signal.
        pass
    (exit_fn or os._exit)(0)


def start_stdin_eof_watcher() -> threading.Thread:
    """Run `_watch_stdin_eof` on a daemon thread (G6 §7.3)."""
    thread = threading.Thread(target=_watch_stdin_eof, name="mfp-stdin-eof", daemon=True)
    thread.start()
    return thread


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind (and listen on) a socket, so the caller can learn the real port.

    `uvicorn.run()` binds internally and never reports the chosen port, so
    `--port 0` cannot be implemented through it (G6 §7.2). Creating the
    socket here and handing it over is what makes an ephemeral port usable.

    **No `SO_REUSEADDR`.** On Windows it does not mean what it means on
    Unix -- it permits *hijacking* a live listener. `SO_EXCLUSIVEADDRUSE` is
    the Windows equivalent of the Unix intent and is the default behaviour
    for a fresh socket anyway, and with `--port 0` neither option buys
    anything at all.

    `listen()` is called here rather than left to asyncio because the ready
    line is emitted immediately afterwards: a shell that connects the
    instant it sees that line would otherwise race a socket that is bound
    but not yet accepting. Calling `listen()` twice is legal (asyncio calls
    it again with its own backlog), so nothing downstream is disturbed.
    """
    hostname = host.strip("[]")
    family = socket.AF_INET6 if ":" in hostname else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((hostname, port))
        sock.listen(2048)
    except BaseException:
        sock.close()
        raise
    return sock


# --- app construction and the blocking entry point --------------------------


def build_app(
    *,
    config: AppConfig | None = None,
    queue_path: Path | None = None,
    gui_dist: Path | None = None,
    with_worker: bool = True,
):
    """Construct the FastAPI app against the user's real state files.

    `with_worker=False` builds the API without a transfer worker. It exists
    because that is a real configuration -- and because `/v1/health` reports
    it rather than hiding it, so a build without one cannot be mistaken for
    a build whose start button is broken.
    """
    from mfp.queue import TaskQueue  # local: keeps import cost off `mfp doctor`
    from mfp.server.app import create_app
    from mfp.server.events import EventBroadcaster
    from mfp.server.worker import TaskWorker

    cfg = config if config is not None else load_config()
    path = queue_path or default_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    queue = TaskQueue(path)
    broadcaster = EventBroadcaster()
    worker = (
        TaskWorker(
            queue=queue,
            config=cfg,
            broadcaster=broadcaster,
            state_dir=path.parent,
        )
        if with_worker
        else None
    )

    return create_app(
        queue=queue,
        config=cfg,
        broadcaster=broadcaster,
        save_config=lambda updated: save_config(updated),
        worker=worker,
        gui_dist=gui_dist,
    )


def check_bind(host: str) -> None:
    from mfp.server.security import LOOPBACK_HOSTS  # local: see the header note

    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackBind(
            f"refusing to bind {host!r}: `mfp serve` has no authentication "
            "(O-3). Only loopback addresses are permitted."
        )


def add_serve_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register every `serve` flag on `parser` (G6 §7.1).

    Lives here, next to the code that consumes the flags, so the CLI and the
    sidecar entry point cannot drift into offering different surfaces. Every
    G6 flag is opt-in; with none of them a bare `mfp serve` behaves exactly
    as it did before this milestone.
    """
    parser.add_argument(
        "--host", default=None, help="Bind address (loopback only; default from config)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default from config: 47821). 0 = an OS-assigned free port",
    )
    parser.add_argument(
        "--ready-json",
        action="store_true",
        help="Print one {\"event\":\"ready\",...} line on stdout once listening",
    )
    parser.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="Exit when stdin closes, i.e. when the parent process dies",
    )
    parser.add_argument(
        "--gui-dist",
        default=None,
        help="Directory holding the built SPA; served at / (mounted after /v1)",
    )
    return parser


def run(
    *,
    host: str | None = None,
    port: int | None = None,
    ready_json: bool = False,
    exit_on_stdin_eof: bool = False,
    gui_dist: Path | str | None = None,
) -> int:
    """Blocking entry point for `mfp serve`. Returns a process exit code."""
    import uvicorn

    config = load_config()
    bind_host = host or config.serve.host
    bind_port = port if port is not None else config.serve.port

    check_bind(bind_host)

    lock = QueueLock(default_queue_lock_path())
    lock.acquire()
    try:
        app = build_app(
            config=config, gui_dist=Path(gui_dist) if gui_dist is not None else None
        )
        sock = bind_socket(bind_host, bind_port)
        try:
            bound_port = sock.getsockname()[1]
            # Started before the ready line so that a parent which dies
            # during startup is still noticed; started after the bind so a
            # failed bind exits on its own error rather than on EOF.
            if exit_on_stdin_eof:
                start_stdin_eof_watcher()
            if ready_json:
                emit_ready(bound_port)
            uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=[sock])
        finally:
            sock.close()
    finally:
        lock.release()
    return 0


__all__ = [
    "NonLoopbackBind",
    "QueueLock",
    "READY_API_VERSION",
    "add_serve_arguments",
    "bind_socket",
    "build_app",
    "check_bind",
    "default_queue_lock_path",
    "default_queue_path",
    "emit_ready",
    "process_is_alive",
    "run",
    "start_stdin_eof_watcher",
]
