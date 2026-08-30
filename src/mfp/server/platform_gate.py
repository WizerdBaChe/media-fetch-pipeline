"""Server-side cache of which platforms are currently blocked.

`capabilities.blocked_platforms()` needs a `doctor` report, and a `doctor`
report costs three subprocess launches. `/input/parse` is called on every
keystroke, so computing it per request would put a process spawn behind the
paste box.

So it is computed once, lazily, and held on `app.state`. `/doctor` refreshes
it, which is not an optimization but the recovery path: a user who installs a
newer yt-dlp opens the Settings panel to confirm, that panel calls `/doctor`,
and the block lifts in the same action that showed them it was fixed. Without
that they would have to restart the app to be believed.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from mfp.capabilities import blocked_platforms
from mfp.doctor import DoctorReport, run_doctor

_STATE_ATTR = "blocked_platforms"


def refresh_blocked_platforms(app: FastAPI, report: DoctorReport) -> dict[str, str]:
    """Recompute the cache from a report someone else already paid for."""
    blocked = blocked_platforms(report)
    setattr(app.state, _STATE_ATTR, blocked)
    return blocked


def current_blocked_platforms(request: Request) -> dict[str, str]:
    """The cached set, computing it on first use.

    A failure to determine it degrades to "nothing is blocked" rather than
    to "everything is blocked". Refusing a paste because our own check
    crashed would take a working download away from the user on no evidence
    at all -- and the transfer layer still reports the real error if the
    platform does refuse.
    """
    app = request.app
    cached = getattr(app.state, _STATE_ATTR, None)
    if cached is not None:
        return cached
    try:
        report = run_doctor(app.state.config)
    except Exception:  # noqa: BLE001 -- see the docstring; never fail closed
        setattr(app.state, _STATE_ATTR, {})
        return {}
    return refresh_blocked_platforms(app, report)
