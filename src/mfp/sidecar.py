"""Entry point for the packaged sidecar binary (`mfp-sidecar.exe`, G6 §4.3).

Why this exists as its own module rather than as flags on `mfp.cli`:

The G6 session was explicitly forbidden from editing `src/mfp/cli.py`, which
another milestone (M8) owns and is editing concurrently. The four flags of
G6 §7.1 therefore live here, on a dedicated entry point, and the argument
surface itself lives in `mfp.serve.add_serve_arguments()` so that wiring the
same flags into `mfp serve` later is a two-line change with no risk of the
two surfaces drifting apart. See the G6 delivery note, "requested changes".

The invocation contract the Electron shell relies on is unchanged from the
spec:

    mfp-sidecar.exe serve --port 0 --ready-json --exit-on-stdin-eof \
                          --gui-dist <resources>/gui

**stdout carries exactly one line ever** -- the ready handshake. Everything
else, including every failure below, goes to stderr. A startup failure is
reported as prose that names its taxonomy code first, so the shell can match
on the code while a human reading a console still gets a sentence:

    serve: queue_locked: another media-fetch-pipeline process (PID 1234) ...

Those tags are startup diagnostics, not wire error codes: only `queue_locked`
and `usage_error` are taxonomy rows. `bind_failed` names the §8.1 "port bind
refused" row, which never reaches a client because there is no server yet.
"""

from __future__ import annotations

import argparse
import sys

from mfp.serve import add_serve_arguments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mfp-sidecar",
        description="media-fetch-pipeline local API sidecar (loopback only)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_serve_arguments(
        subparsers.add_parser("serve", help="Run the local HTTP API (loopback only)")
    )
    return parser


def _fail(code: str, message: str) -> None:
    print(f"serve: {code}: {message}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    from mfp.errors import QueueLocked
    from mfp.serve import NonLoopbackBind, run

    args = build_parser().parse_args(argv)

    try:
        return run(
            host=args.host,
            port=args.port,
            ready_json=args.ready_json,
            exit_on_stdin_eof=args.exit_on_stdin_eof,
            gui_dist=args.gui_dist,
        )
    except NonLoopbackBind as exc:
        _fail("usage_error", str(exc))
        return 2
    except QueueLocked as exc:
        _fail("queue_locked", str(exc))
        return 1
    except ValueError as exc:
        # The only ValueError `run()` raises is the §7.4 gui_dist guard --
        # a missing or empty SPA directory, which would otherwise become a
        # blank window (batch 1 §14.1 forbids that outcome).
        _fail("usage_error", str(exc))
        return 2
    except OSError as exc:
        # Bind refused, port already taken, no permission to open a socket.
        _fail("bind_failed", f"could not open a local port: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
