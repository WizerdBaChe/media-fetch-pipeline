"""FastAPI application factory for `mfp serve` (PSM Batch 2 GUI §4.1).

Loopback-only, unauthenticated. That is a deliberate, recorded limitation
(O-3): binding anywhere other than 127.0.0.1 -- or exposing this through a
tunnel -- requires O-3 to be ruled first. `create_app()` takes its
dependencies as arguments so tests construct it with a temp-dir queue and a
fake clock, and never touch the user's real state.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from mfp.config import AppConfig
from mfp.errors import MfpError
from mfp.queue import IllegalTransition, TaskNotFound, TaskQueue
from mfp.server.events import EventBroadcaster
from mfp.server.routes_brief import build_brief_router
from mfp.server.routes_config import build_config_router
from mfp.server.routes_queue import build_queue_router
from mfp.server.routes_stack import build_stack_router
from mfp.server.routes_tools import build_tools_router
from mfp.server.stack_jobs import StackJobRunner
from mfp.server.security import install_loopback_guard

API_PREFIX = "/v1"

#: PSM Batch 2 §11. Anything absent maps to 500 -- an unmapped error is a
#: defect to fix in the taxonomy, not something to paper over with a 400.
#: How often the O-6 sweep runs after the one at startup. An hour is the
#: spec's own number and it is not tuning-sensitive: the setting is measured
#: in DAYS, so the only thing a shorter interval buys is finding the same
#: rows sooner on the one day they age out.
AUTO_CLEAR_INTERVAL_S = 3600.0


def sweep_completed(app_: FastAPI) -> None:
    """Drop COMPLETED records older than the configured age (O-6, B-2).

    `queue.sweep` has existed since O-6 was ruled and nothing ever called
    it, so the setting was a switch wired to nothing. Records only -- INV-6
    says removing a record never deletes what was downloaded, and an
    unattended sweep is the last place that promise should be tested.

    Announced through the SAME `records_removed` notice a manual clear
    uses, because from the user's side it is the same event; a sweep that
    happened in silence would look like rows going missing.
    """
    days = getattr(app_.state.config, "auto_clear_days", 0)
    if not days:
        return
    result = app_.state.queue.sweep(max_age_days=days)
    if not result.removed_ids:
        return
    broadcaster = app_.state.broadcaster
    broadcaster.publish("removed", {"ids": result.removed_ids})
    broadcaster.publish(
        "notice",
        {
            "level": "info",
            "code": "records_removed",
            "removed": result.removed,
            "cancelledInFlight": result.cancelled_in_flight,
        },
    )


async def _auto_clear_loop(app_: FastAPI) -> None:
    """Once at startup, then hourly. Re-reads the config every pass so a
    change in the Settings panel takes effect without a restart."""
    while True:
        try:
            sweep_completed(app_)
        except Exception:  # noqa: BLE001 - a sweep must never kill the server
            pass
        await asyncio.sleep(AUTO_CLEAR_INTERVAL_S)


ERROR_STATUS: dict[str, int] = {
    "usage_error": 400,
    # 400 beside `usage_error`, not 404: the status is about the REQUEST, and
    # this request is as malformed as one naming a nonexistent field -- it
    # names a path that is not there. 404 is reserved here for a resource the
    # server itself keeps (`task_not_found`, `stack_job_not_found`), and
    # sharing it would say the server had lost something of its own. What
    # separates this from `usage_error` is the sentence the GUI shows, which
    # is the whole reason it is a distinct code (UX walkthrough F1).
    "source_not_found": 400,
    "unsupported_url": 400,
    "path_too_long": 400,
    # 400 with the other "your argument cannot be used" failures: `--out`
    # pointed an analysis artifact outside every store (`INV-P3`). Nothing is
    # broken and retrying unchanged will fail identically.
    "outside_store": 400,
    "task_not_found": 404,
    "stack_job_not_found": 404,
    "illegal_transition": 409,
    "link_expired": 410,
    "rate_limited": 429,
    "budget_exhausted": 429,
    "login_wall": 403,
    # The platform's refusal, forwarded verbatim. Not 502: nothing upstream
    # is broken -- it answered, and the answer was no.
    "platform_transfer_blocked": 403,
    "dependency_missing": 503,
    # 502: the failure is almost always upstream of us -- a release page
    # that moved, a checksum that did not match, a host that would not
    # answer -- and the panel's response is to offer a retry and a manual
    # link. Sharing 503 with `dependency_missing` would blur "the tool is
    # not here" into "fetching the tool did not work", which are different
    # things to be told.
    "tool_install_failed": 502,
    "chrome_default_profile": 500,
    "upstream_structure_change": 502,
    # 504, deliberately not 502: 502 is "the upstream answered and the answer
    # was broken", which is what a structure change is. This one is "the
    # upstream did not answer at all, or answered with its own 5xx" -- a
    # timeout in the literal sense, and the one status that says retry later
    # without also saying something here is wrong.
    "upstream_unreachable": 504,
    # 422, deliberately not 502: the post was read successfully and simply
    # has nothing to download. Nothing upstream is broken, so it must not
    # share a status with the code that means our parser is now wrong.
    "no_media_in_post": 422,
    # 422 again, not 502 (P-94): the platform answered cleanly, with its own
    # "this post cannot be shown" page. A fact about the URL, not a fault
    # upstream and not our parser.
    "post_unavailable": 422,
    # The `stack` family. 422 for the same reason `no_media_in_post` is:
    # the video was read fine and does not hold what was asked for, which is
    # a fact about the request, not a fault upstream or here.
    "nothing_to_stack": 422,
    "no_subtitle_pixels_in_band": 422,
    # 500: ffmpeg is installed and ran -- this machine failed at the work.
    # Not 503, which is reserved for a dependency that is not there at all.
    "media_tool_failed": 500,
    # 500: the pictures fetched fine and THIS machine failed to write the
    # explanation beside them -- a directory, a permission or a full disk.
    # Not 4xx: the request was valid and re-sending it unchanged may well
    # succeed once the disk does.
    "analysis_write_failed": 500,
    "media_transfer_failed": 502,
    # A strategy-level signal that normally makes the adapter fall through to
    # the next strategy (PSM §5.1). If one ever escapes to a client it is an
    # upstream failure, not our bug -- 502, same as the other upstream rows.
    "cdp_timeout": 502,
    "path_escape": 400,
    # G6 §5.1. Detected before the socket is bound, so no client can actually
    # receive it over HTTP -- the row exists so the taxonomy stays complete.
    "queue_locked": 409,
    # Never emitted by this process; the client invents it when the request
    # did not reach us at all. Listed so the table stays the single
    # description of what a client can receive.
    "server_unreachable": 503,
    # Emitted by the loopback guard (O-3), not by MfpError -- listed so the
    # table stays the single description of what the client can receive.
    "forbidden_host": 403,
    "cross_origin_denied": 403,
}


def capabilities_for(worker: object | None) -> dict[str, bool]:
    """What this build can actually do, reported by `GET /v1/health`.

    Computed from the running app rather than declared as a constant, and
    that is the whole point: the flag answers "can the GUI's start button do
    anything", not "does the module exist". A queue can hold a task in
    PROBING or DOWNLOADING with nothing advancing it, and a GUI that starts
    such a task looks wedged -- indistinguishable from a bug. An app built
    without a worker (every test, and anything wanting the API without a
    downloader) says so honestly instead.

    M4 landed the transfer engine and verified it against live posts while
    this still read false, because the engine existing was never the
    question. M8's worker is what changed the answer.
    """
    live = worker is not None
    return {"probe": live, "download": live}


def create_app(
    *,
    queue: TaskQueue,
    config: AppConfig,
    broadcaster: EventBroadcaster | None = None,
    save_config: Callable[[AppConfig], None] | None = None,
    worker: object | None = None,
    gui_dist: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # The way back onto this loop, for anything that publishes from a
        # thread. The worker and the stack runner each take their own copy
        # through `bind_dispatch`; a plain `def` endpoint has no such hook,
        # because FastAPI puts it in a threadpool without telling it, so it
        # reads this instead. Same guarantee either way: the broadcaster's
        # asyncio queues are only ever touched from the loop that owns them.
        app_.state.dispatch = asyncio.get_running_loop().call_soon_threadsafe
        if worker is not None:
            # Every queue mutation the worker makes is posted back to this
            # loop, so the queue keeps exactly one writer. Bound here
            # because this is the first moment the loop exists.
            worker.bind_dispatch(asyncio.get_running_loop().call_soon_threadsafe)
            # And the settings, which `PUT /v1/config` REBINDS rather than
            # mutates. Without this the worker keeps whatever was true when
            # the process started -- see `TaskWorker._config`.
            worker.bind_config(lambda: app_.state.config)
            worker.start()
        stack_runner = app_.state.stack_runner
        stack_runner.bind_dispatch(asyncio.get_running_loop().call_soon_threadsafe)
        stack_runner.start()
        # The run log is swept where every other periodic job runs, and for
        # the same reason `_auto_clear_loop` re-reads its setting each pass:
        # a change in Settings must not need a restart.
        sweeper = asyncio.create_task(_auto_clear_loop(app_))
        yield
        sweeper.cancel()
        stack_runner.stop()
        if worker is not None:
            worker.stop()

    app = FastAPI(
        title="media-fetch-pipeline",
        version="2.0.2",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.queue = queue
    app.state.config = config
    app.state.broadcaster = broadcaster or EventBroadcaster()
    app.state.save_config = save_config
    app.state.worker = worker
    # Always present, unlike `worker`: a stack job needs no network, no
    # budget slot and no browser, so there is no build in which the API can
    # honestly offer the routes and not run them.
    app.state.stack_runner = StackJobRunner(
        config=config,
        on_event=lambda event, data: app.state.broadcaster.publish(event, data),
    )

    # O-3: registered first so it wraps every route, including /v1/health.
    install_loopback_guard(app)

    @app.exception_handler(MfpError)
    async def _mfp_error_handler(_request: Request, exc: MfpError) -> JSONResponse:
        status = ERROR_STATUS.get(exc.error_code, 500)
        return JSONResponse(
            status_code=status,
            content={
                "errorCode": exc.error_code,
                "message": str(exc),
                "detail": exc.detail,
            },
        )

    app.include_router(build_queue_router(), prefix=API_PREFIX)
    app.include_router(build_stack_router(), prefix=API_PREFIX)
    app.include_router(build_config_router(), prefix=API_PREFIX)
    app.include_router(build_brief_router(), prefix=API_PREFIX)
    app.include_router(build_tools_router(), prefix=API_PREFIX)

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, object]:
        return {
            "ok": True,
            "apiVersion": 1,
            "capabilities": capabilities_for(request.app.state.worker),
            # PSM §4.5: an unreadable queue file rebuilds empty rather than
            # crashing, but the user must be told -- otherwise their whole
            # queue vanishes with no account of why.
            "queueRebuilt": request.app.state.queue.load_degraded,
            # How many event streams this process is currently feeding. Not
            # used by the UI; it is here because diagnosing R5-15 meant
            # counting sockets with netstat, which is a poor substitute for
            # the server simply saying how many clients it thinks it has.
            "streamSubscribers": request.app.state.broadcaster.subscriber_count,
        }

    # G6 §7.4 -- the SPA, mounted LAST, after both /v1 routers and after
    # /v1/health, so a file in the dist directory can never shadow an API
    # path. Starlette matches mounts in registration order, so moving this
    # call earlier would silently take over /v1;
    # `tests/unit/test_server_static.py` asserts /v1/health still answers
    # JSON with a gui_dist present, because that ordering is exactly what a
    # later refactor breaks without noticing.
    #
    # A missing or empty directory is a HARD error, not a silent skip: an
    # Electron window pointed at a server with no SPA renders a blank page,
    # which batch 1 §14.1 forbids outright.
    if gui_dist is not None:
        # Imported here rather than at module scope: nothing but this branch
        # needs it, and the import block above is shared with M8.
        from fastapi.staticfiles import StaticFiles

        index = Path(gui_dist) / "index.html"
        if not index.is_file():
            raise ValueError(
                f"gui_dist {str(gui_dist)!r} contains no index.html -- refusing to "
                "start a server that would render a blank window"
            )
        app.mount("/", StaticFiles(directory=str(gui_dist), html=True), name="gui")

    return app


def create_app_from_paths(
    *, queue_path: Path, config: AppConfig, save_config: Callable[[AppConfig], None] | None = None
) -> FastAPI:
    return create_app(queue=TaskQueue(queue_path), config=config, save_config=save_config)


__all__ = [
    "API_PREFIX",
    "ERROR_STATUS",
    "IllegalTransition",
    "TaskNotFound",
    "capabilities_for",
    "create_app",
    "create_app_from_paths",
]
