"""Loopback guard for `mfp serve` (PSM Batch 2 GUI §4.1, O-3).

Binding to 127.0.0.1 keeps other machines out. It does NOT keep *browsers*
out, and that gap was measured, not assumed -- against the unguarded app,
three attacks a web page can mount succeeded outright:

  1. **CSRF.** `POST /v1/queue:clearAll` needs no body and no custom header,
     so any page the user visits can submit it cross-origin. Measured: the
     queue went from 1 task to 0, cancelling in-flight work.
  2. **DNS rebinding.** A hostile domain resolving to 127.0.0.1 turns the
     browser into a proxy for the attacker. Measured: `GET /v1/config` with
     a foreign `Host` header returned 200 and disclosed `outputRoot`.
  3. **Simple-request bypass.** `Content-Type: text/plain` avoids a CORS
     preflight entirely, so "the browser will preflight it" is not a
     defence. Measured: 200.

This middleware closes all three with header validation rather than a token:

- `Host` must name a loopback address, which is what defeats rebinding --
  the attacker controls DNS but not the header the browser sends.
- A cross-origin `Origin`, or a `Sec-Fetch-Site` of `cross-site`, is
  rejected outright.

Non-browser callers (the Agent-facing Skill, curl, the Electron shell) send
neither `Origin` nor `Sec-Fetch-Site`, so they pass untouched. That is the
reason to prefer this over a shared token: it costs the legitimate
programmatic consumer nothing, while a token would have to be read from
config by every caller and would not stop rebinding on its own.

**Out of scope, deliberately**: a hostile *local process*. On a single-user
desktop such a process can read `config.json` and the output tree directly,
so a token on this port would not raise the bar. Remote exposure (EXT-6)
remains blocked -- it needs real authentication, not this.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp

#: Host header values that denote this machine.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: `Sec-Fetch-Site` values a browser sends for requests we accept. Anything
#: else -- notably "cross-site" -- is a page attacking us, not our own UI.
ALLOWED_FETCH_SITES: frozenset[str] = frozenset({"same-origin", "same-site", "none"})


def _hostname_of(value: str) -> str:
    """Extract the bare hostname from a `Host` header or an origin URL."""
    candidate = value.strip()
    if "://" in candidate:
        return (urlsplit(candidate).hostname or "").lower()
    # Bracketed IPv6 literal: keep the brackets, drop any port after them.
    if candidate.startswith("["):
        end = candidate.find("]")
        if end != -1:
            return candidate[: end + 1].lower()
        return candidate.lower()
    # A bare IPv6 literal ("::1") has several colons and cannot carry a
    # port -- splitting on the last colon would mangle it into ":".
    if candidate.count(":") > 1:
        return candidate.lower()
    return candidate.rsplit(":", 1)[0].lower() if ":" in candidate else candidate.lower()


def _deny(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"errorCode": code, "message": message, "detail": None},
    )


def check_request(headers: dict[str, str]) -> JSONResponse | None:
    """Return a denial response, or None when the request may proceed.

    Split out from the middleware so the policy is testable as a pure
    function, independent of ASGI plumbing.
    """
    host = headers.get("host")
    if host is not None and _hostname_of(host) not in LOOPBACK_HOSTS:
        return _deny(
            "forbidden_host",
            "request Host header does not name a loopback address "
            "(DNS-rebinding protection)",
        )

    origin = headers.get("origin")
    if origin is not None and origin != "null" and _hostname_of(origin) not in LOOPBACK_HOSTS:
        return _deny("cross_origin_denied", f"cross-origin request from {origin} refused")

    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in ALLOWED_FETCH_SITES:
        return _deny("cross_origin_denied", f"cross-site request refused ({fetch_site})")

    return None


def install_loopback_guard(app: ASGIApp) -> None:
    """Register the guard as the outermost HTTP middleware."""

    @app.middleware("http")  # type: ignore[attr-defined]
    async def _loopback_guard(request: Request, call_next):
        denial = check_request({k.lower(): v for k, v in request.headers.items()})
        if denial is not None:
            return denial
        return await call_next(request)
