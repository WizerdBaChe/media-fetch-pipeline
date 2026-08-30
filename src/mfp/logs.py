"""What this program did, in a file, and what it did wrong, in another one.

Until now the only record of a run was the prose on stderr, which exists
only as long as the terminal that saw it. The GUI made that worse rather
than better: a window has no scrollback worth the name, and "it failed
yesterday" had nothing behind it at all.

Three rules shape everything here, and each one is a defect avoided:

1. **Actions and external programs are recorded; queries are not.** A line
   per `GET /queue` or per SSE tick would bury the twenty lines that matter
   under thousands that never do, and a log nobody can read is the same as
   no log while costing disk. What earns a line is a verb that CHANGES
   something, and every subprocess this program starts.

2. **A failure gets a folder, not a line.** The line says a run failed; the
   bundle beside it carries the parameters, the full stderr rather than the
   three-line tail an error message can hold, the equivalent command to
   reproduce it, and whatever frames the run kept as evidence. Bundles are
   never touched by the retention sweep -- the whole point of separating
   them is that the interesting half must outlive the routine half.

3. **Logging never breaks the product.** Every entry point here swallows its
   own IO errors. A download that works must not become a download that
   crashes because a log directory went read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from mfp.config import app_data_dir

#: Bumped when the shape of a line changes. Readers (the GUI's log panel, a
#: person with `jq`) branch on it rather than on the absence of a key.
SCHEMA_VERSION = 1

_LOCK = threading.Lock()
_ROOT: Path | None = None
_SILENCED = False

#: The action line currently being built, for `annotate` to reach. A
#: ContextVar rather than a global: the sidecar serves requests concurrently,
#: and two actions in flight must not write into each other's line.
_CURRENT: ContextVar[dict | None] = ContextVar("mfp_log_action", default=None)


# ---------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------
def log_root(root: Path | None = None) -> Path:
    """`%APPDATA%/media-fetch-pipeline/logs`, or whatever was configured.

    Beside the config and the queue rather than beside the downloads: a log
    describes the PROGRAM, and the output root is the user's own folder --
    one they sync, share, and clean out.
    """
    return Path(root or _ROOT or (app_data_dir() / "logs"))


def error_root(root: Path | None = None) -> Path:
    return log_root(root) / "errors"


def configure(root: Path | None) -> None:
    """Point the process's logging at `root` (tests, and the sidecar)."""
    global _ROOT
    _ROOT = Path(root) if root else None


def action_log_path(root: Path | None = None, *, now: datetime | None = None) -> Path:
    """One file per local day.

    Local rather than UTC because the person reading it is looking for "what
    happened last night", and a file that rolls at 08:00 their time answers a
    question nobody asked.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    return log_root(root) / f"actions-{stamp}.jsonl"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def safe_url(url: str | None) -> str | None:
    """A URL with its query string removed.

    Platform media URLs are SIGNED: `oe=`/`oh=` on Instagram, a `sig=` on
    googlevideo. Those are short-lived credentials for somebody's bytes, and
    a log is a file people attach to bug reports. The path identifies the
    post well enough to follow what happened; the query is the part that must
    not travel.
    """
    if not url:
        return url
    head, sep, _ = url.partition("?")
    return head + ("?…" if sep else "")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def record(event: str, *, root: Path | None = None, **fields: Any) -> None:
    """Append one line. Never raises."""
    line = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "schema": SCHEMA_VERSION,
        "pid": os.getpid(),
        "event": event,
    }
    line.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = action_log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - the failure mode, not the path
        _complain_once(exc)


def _complain_once(exc: Exception) -> None:
    """Say it once on stderr and then stop.

    A logger that cannot write is worth knowing about; a logger that says so
    on every line has replaced the flood it was meant to prevent.
    """
    global _SILENCED
    if _SILENCED:
        return
    _SILENCED = True
    import sys

    print(f"note: could not write the run log ({exc})", file=sys.stderr)


@contextmanager
def action(verb: str, *, root: Path | None = None, **fields: Any) -> Iterator[dict]:
    """Record one action when it finishes, with how long it took.

    One line at the end rather than a start/end pair: the pair doubles the
    volume to say the same thing, and nothing here is long enough to need a
    "still running" marker in the FILE -- the GUI has live events for that.

    The yielded dict is the line's payload: fill it in as the action learns
    what it did (`payload["strips"] = 24`), and it is written either way.
    Failures carry the exception type and message, and whatever bundle the
    caller wrote.

    Setting `payload["ok"] = False` marks an action that failed and was
    HANDLED -- the CLI turns a taxonomy error into an exit code rather than
    letting it escape, and without this every handled failure would be
    recorded as a success.
    """
    payload: dict[str, Any] = dict(fields)
    started = time.monotonic()
    token = _CURRENT.set(payload)
    try:
        yield payload
    except BaseException as exc:
        payload.setdefault("error", type(exc).__name__)
        payload.setdefault("detail", str(exc)[:400])
        payload.pop("ok", None)
        record(verb, root=root, ok=False, ms=_ms(started), **payload)
        raise
    else:
        verdict = bool(payload.pop("ok", True))
        record(verb, root=root, ok=verdict, ms=_ms(started), **payload)
    finally:
        _CURRENT.reset(token)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def annotate(**fields: Any) -> None:
    """Add to the action line being built by the enclosing `action()`.

    Exists so the outcome can be recorded where it is KNOWN. The alternative
    is a second "result" line written by the code that has the numbers, and
    two lines describing one action is how a reader ends up counting the same
    run twice.

    A no-op outside an action, and in a thread that did not inherit the
    context: an annotation that cannot land is not worth an exception in
    somebody's download.
    """
    payload = _CURRENT.get()
    if payload is not None:
        payload.update(fields)


def ran(program: str, *, args: Sequence[str] = (), code: int | None = 0, ms: int = 0,
        root: Path | None = None, **fields: Any) -> None:
    """One external program, run once. `args` is summarised, never the URL.

    `code=None` means the program never started -- it is not installed, or not
    on PATH. Recorded as null rather than as some sentinel integer, because
    "no exit code" and "exit code 0" are different facts and a reader of the
    log should not have to know which number we chose to mean the first one.
    """
    record(
        "process",
        root=root,
        program=Path(program).stem,
        args=" ".join(safe_url(a) or "" for a in args)[:400],
        code=code,
        ms=ms,
        ok=code == 0,
        **fields,
    )


# ---------------------------------------------------------------------------
# Error bundles
# ---------------------------------------------------------------------------
@dataclass
class ErrorBundle:
    path: Path
    name: str


def write_error_bundle(
    verb: str,
    *,
    code: str,
    detail: str,
    params: dict | None = None,
    command: Sequence[str] | str | None = None,
    stderr: str | None = None,
    evidence: Sequence[Path] = (),
    extra_files: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> ErrorBundle | None:
    """Everything needed to understand one failure, in its own folder.

    `command` is the point of the exercise: a failure a person can re-run in
    a terminal is a failure that can be argued with. The evidence files are
    copied rather than referenced -- a work directory is cleaned up, and a
    bundle that points at a deleted frame is a bundle that lies.

    `extra_files` is `{filename: text}` for content that exists NOWHERE ELSE
    once the command has failed, as opposed to `evidence`, which copies files
    already on disk. `brief-save` is why it exists: its body arrives on stdin
    only, deliberately (PowerShell 5.1 truncates an argument containing `"`
    or `[`, silently), so a failed append destroys the one expensive artifact
    in the run. Text, not paths -- if the caller had a path it would use
    `evidence`.
    """
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    safe_code = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)[:40]
    directory = error_root(root) / f"{stamp}_{verb}_{safe_code}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "verb": verb,
                    "code": code,
                    "detail": detail,
                    "params": params or {},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if command:
            text = command if isinstance(command, str) else " ".join(
                _quote(part) for part in command
            )
            (directory / "command.txt").write_text(text + "\n", encoding="utf-8")
        if stderr:
            (directory / "stderr.txt").write_text(stderr, encoding="utf-8")
        for name, body in (extra_files or {}).items():
            # Written with an explicit LF newline for the same reason
            # `brief.append_entry` does: the commonest caller hands over
            # markdown the user may reopen, and a bundle that rewrites the
            # line endings of the text it rescued is a bundle that altered
            # the evidence. `Path(name).name` because the caller names the
            # file and a caller-supplied `..\\` must not escape the bundle.
            (directory / Path(name).name).write_text(
                body, encoding="utf-8", newline="\n"
            )
        kept = directory / "evidence"
        for source in evidence:
            source = Path(source)
            if not source.is_file():
                continue
            kept.mkdir(exist_ok=True)
            shutil.copy2(source, kept / source.name)
    except OSError as exc:  # pragma: no cover - the failure mode, not the path
        _complain_once(exc)
        return None
    return ErrorBundle(path=directory, name=directory.name)


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
@dataclass
class SweepReport:
    """What a sweep removed. Reported rather than assumed: the GUI shows it,
    and a delete button that says nothing is a delete button nobody trusts."""

    files: int = 0
    bundles: int = 0
    bytes_freed: int = 0
    kept_today: bool = False
    errors: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "files": self.files,
            "bundles": self.bundles,
            "bytesFreed": self.bytes_freed,
            "keptToday": self.kept_today,
        }


def sweep_action_logs(retention_days: int, *, root: Path | None = None,
                      now: datetime | None = None) -> SweepReport:
    """Delete daily logs older than `retention_days`. Never bundles.

    `0` disables the sweep, matching `auto_clear_days` -- one convention for
    "off" across the settings panel rather than two.

    Today's file is never eligible, whatever the number says: it is the one
    another part of this process is appending to, and deleting a log to make
    room for the log being written is the kind of cleverness that loses the
    session that mattered.
    """
    report = SweepReport(kept_today=True)
    if retention_days <= 0:
        return report
    now = now or datetime.now()
    cutoff = (now - timedelta(days=retention_days)).date()
    today = action_log_path(root, now=now).name
    for path in _daily_logs(root):
        if path.name == today:
            continue
        stamp = path.stem.removeprefix("actions-")
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d").date()
        except ValueError:
            continue  # not ours to delete
        if when > cutoff:
            continue
        _remove(path, report)
    return report


def clear_action_logs(*, root: Path | None = None) -> SweepReport:
    """Delete every daily log, today's included.

    Unlike the sweep, this one is somebody pressing a button that says delete
    the logs, and keeping today's back would be answering a different
    question. The deletion itself is then recorded -- into a file this call
    just removed, which is deliberate: the new file starts with the reason
    it is empty.
    """
    report = SweepReport()
    for path in _daily_logs(root):
        _remove(path, report)
    record("logs.cleared", root=root, files=report.files,
           bytesFreed=report.bytes_freed)
    return report


def clear_error_bundles(*, root: Path | None = None) -> SweepReport:
    """Delete every error bundle. Only ever on request.

    There is no automatic version of this on purpose: bundles are kept until
    a person says otherwise, because the failure they describe is usually
    noticed long after the day it happened.
    """
    report = SweepReport()
    base = error_root(root)
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            try:
                shutil.rmtree(child)
            except OSError as exc:
                report.errors.append(f"{child.name}: {exc}")
                continue
            report.bundles += 1
            report.bytes_freed += size
    record("logs.errorsCleared", root=root, bundles=report.bundles,
           bytesFreed=report.bytes_freed)
    return report


def usage(*, root: Path | None = None) -> dict:
    """How much there is to delete, for a button that names its own cost."""
    files = _daily_logs(root)
    bundles = [p for p in error_root(root).iterdir() if p.is_dir()] \
        if error_root(root).is_dir() else []
    return {
        "logDir": str(log_root(root)),
        "errorDir": str(error_root(root)),
        "files": len(files),
        "fileBytes": sum(p.stat().st_size for p in files),
        "bundles": len(bundles),
        "bundleBytes": sum(
            f.stat().st_size for b in bundles for f in b.rglob("*") if f.is_file()
        ),
    }


def _daily_logs(root: Path | None) -> list[Path]:
    base = log_root(root)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir()
                  if p.is_file() and p.name.startswith("actions-")
                  and p.suffix == ".jsonl")


def _remove(path: Path, report: SweepReport) -> None:
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError as exc:
        report.errors.append(f"{path.name}: {exc}")
        return
    report.files += 1
    report.bytes_freed += size


def tail(limit: int = 200, *, root: Path | None = None) -> list[dict]:
    """The last `limit` lines of today's log, parsed.

    A line that does not parse is skipped rather than raising: a log is
    append-only from more than one process, and a torn final line must not
    stop the reader from seeing the 199 whole ones.
    """
    path = action_log_path(root)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:  # pragma: no cover
        _complain_once(exc)
        return []
    out: list[dict] = []
    for line in raw[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
