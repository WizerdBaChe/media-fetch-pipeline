"""Running ffmpeg, ffprobe and yt-dlp, and turning their failures into ours.

Split out of `stack.py` (2026-08-30). `captions.py` shells out to yt-dlp and
`stack.py` shells out to ffmpeg, and they were sharing this through the
second one -- which is why `stack` still had four importers after the cue
parser moved out. Thirty lines of subprocess handling copied into both would
have been worse: the two copies start disagreeing about what a missing
binary is, and that distinction was a defect once already.

Two failures, deliberately different: a program that is NOT INSTALLED is
`DependencyMissingError` and sends the reader to `mfp doctor`; a program
that ran and REFUSED is `MediaToolFailed` and sends them to the stderr this
attaches to the error bundle.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence

from mfp import logs
from mfp.errors import DependencyMissingError, MediaToolFailed, StackError

__all__ = ["missing_tool", "resolved_command", "run_tool", "with_evidence"]


def missing_tool(program: str) -> DependencyMissingError:
    """The one sentence every 'that program is not here' failure says.

    Shared because there were two of them and they disagreed: `run_tool`
    raised this, and `stack._run_progress` -- the pass that does the actual
    work -- let a raw `FileNotFoundError` out, so the long ffmpeg run was
    the ONE ffmpeg call that produced a traceback instead of an error code.
    """
    return DependencyMissingError(
        f"{Path(program).stem} is not installed or not on PATH; "
        f"run `mfp doctor` to see what is missing"
    )


def resolved_command(cmd: list[str]) -> list[str]:
    """Turn a bare program name into the copy this machine should run.

    Callers here write `["ffmpeg", ...]` and `["ffprobe", ...]` because that
    is what the command reads as. Which ffmpeg that means is not their
    business and never was: it is the configured one, or the one this
    program installed into its own tools directory, or whatever is on PATH,
    in that order (`mfp.toolchain`).

    A path that already has a separator in it is left exactly as it is --
    those come from a caller that has resolved the question itself, and
    re-resolving would override an explicit choice.
    """
    from mfp import toolchain

    head = cmd[0]
    if "/" in head or "\\" in head:
        return cmd
    return [toolchain.resolve(head) or head, *cmd[1:]]


def run_tool(cmd: list[str]) -> str:
    cmd = resolved_command(cmd)
    # `stdin=DEVNULL` is the rule the whole product follows and the one
    # `test_subprocess_stdin.py` enforces: a child that inherits our stdin
    # can block forever when something upstream is watching that handle.
    # Independently seen while building this feature -- a section download
    # spawned through ffmpeg sat for eight minutes at ~0 CPU with no output
    # file, and had to be killed.
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError as exc:
        # "the program is not here" is a different state from "the program
        # ran and failed", and only the second one was modelled. Without this
        # a machine with no yt-dlp got a raw FileNotFoundError: a traceback on
        # the CLI, and over HTTP a 500 with no `errorCode` at all -- which
        # breaks the contract that every failure carries one. `mfp doctor`
        # has reported this exact condition as `dependency_missing` since M1;
        # the paths that USE the tool just never said it.
        logs.ran(cmd[0], args=cmd[1:], code=None, ms=0)
        raise missing_tool(cmd[0]) from exc
    logs.ran(cmd[0], args=cmd[1:], code=proc.returncode,
             ms=int((time.monotonic() - started) * 1000))
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        # The message gets three lines because that is what a person can read
        # in a terminal; the error bundle gets all of it, because three lines
        # is rarely where the cause is.
        raise with_evidence(
            MediaToolFailed(f"{Path(cmd[0]).stem} failed: {' / '.join(tail)}"),
            command=cmd, stderr=proc.stderr,
        )
    return proc.stdout


def with_evidence(exc: StackError, *, command: Sequence[str],
                   stderr: str | None, evidence: Sequence[Path] = ()) -> StackError:
    """Hang the forensics off the exception for whoever writes the bundle.

    Attributes rather than `MfpError(**context)`: context is serialised into
    `errorDetail` payloads, and a full ffmpeg stderr does not belong on a
    wire format. The bundle's folder is named from `exc.error_code`, which
    is why the two causes worth telling apart are their own classes.
    """
    exc.log_command = list(command)
    exc.log_stderr = stderr
    exc.log_evidence = list(evidence)
    return exc
