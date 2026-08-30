"""Who owns the browser's lifetime.

`_try_chrome` used to open a `chrome_session` around each page read, which
is right for one CLI invocation and wrong for a queue: every task paid the
4-6 s launch measured in spike-02 §1, and `capture.py` had already said so
in a comment ("One browser for the whole batch: launching per URL would
multiply the 4-6s startup by the number of posts, for nothing"). That
batching existed only on the CLI's capture path; the queue worker never
reached it.

Making the adapter hold a browser open would have been the smaller diff and
the wrong shape: the adapter does not know when the work stops, and
`chrome_session`'s `finally` -- the guarantee behind M2's "zero orphan
Chrome processes after a run" -- would then have no owner. So the lifetime
becomes an explicit collaborator instead. Two implementations, one
interface:

  PerCall   open, read, close. What the CLI and every test get, and the
            behaviour that shipped.
  Shared    open on first use, hold it, close when told. What the worker
            gets, because the worker is the only caller that knows the
            queue has run dry.

Serial navigation (D-36) is untouched: one browser, one page at a time.
Reusing the session is if anything friendlier to the thing D-33's pacing
guard protects against -- a browser that stays open across several posts
looks more like a person than one that relaunches per URL.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Protocol

from mfp.config import AppConfig
from mfp.adapters.instagram.chrome import (
    ChromeSession,
    Connector,
    Launcher,
    chrome_session,
)


class ChromeSessions(Protocol):
    """Hands out a live `ChromeSession` for the duration of a `with`."""

    @contextmanager
    def session(self, config: AppConfig) -> Iterator[ChromeSession]:
        ...

    def close(self) -> None:
        """Release anything held. Idempotent, and safe to call when nothing
        is open -- the caller should never have to ask first."""


class PerCallChromeSessions:
    """One browser per page read: launch, read, tear down.

    The behaviour that shipped, kept as a real implementation rather than a
    special case so the CLI, the tests and any caller with a single URL are
    running the same code path they always were.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        connector: Connector,
        http_get: Callable[[str], str],
        launcher: Launcher,
    ) -> None:
        self._state_dir = state_dir
        self._connector = connector
        self._http_get = http_get
        self._launcher = launcher

    @contextmanager
    def session(self, config: AppConfig) -> Iterator[ChromeSession]:
        with chrome_session(
            config.chrome,
            state_dir=self._state_dir,
            launcher=self._launcher,
            connector=self._connector,
            http_get=self._http_get,
        ) as live:
            yield live

    def close(self) -> None:
        # Nothing is held between calls; `chrome_session`'s own `finally`
        # has already run by the time each `session()` returns.
        return None


class SharedChromeSession:
    """One browser across many page reads, closed on demand.

    The reason this is not simply "keep the context manager open": a
    `with` block cannot span the calls that need it. The generator is
    driven by hand instead, and every exit path -- normal, exceptional,
    and `close()` -- routes through the same teardown, because the orphan
    guarantee is the whole reason `chrome_session` has a `finally`.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        connector: Connector,
        http_get: Callable[[str], str],
        launcher: Launcher,
    ) -> None:
        self._state_dir = state_dir
        self._connector = connector
        self._http_get = http_get
        self._launcher = launcher
        self._open: object | None = None
        self._live: ChromeSession | None = None

    @property
    def is_open(self) -> bool:
        return self._live is not None

    @contextmanager
    def session(self, config: AppConfig) -> Iterator[ChromeSession]:
        if self._live is None:
            manager = chrome_session(
                config.chrome,
                state_dir=self._state_dir,
                launcher=self._launcher,
                connector=self._connector,
                http_get=self._http_get,
            )
            # `__enter__` can raise (no Chrome, default-profile refusal); the
            # manager is only remembered once it has actually produced a
            # session, so a failed launch cannot leave a half-open one to be
            # closed later.
            live = manager.__enter__()
            self._open, self._live = manager, live

        try:
            yield self._live
        except BaseException:
            # A failed page read says nothing certain about the browser, but
            # the cheap and safe reading is that it is no longer trustworthy.
            # Dropping it costs one relaunch; keeping it risks every
            # subsequent read failing the same way with no way out.
            self.close()
            raise

    def close(self) -> None:
        manager, self._open, self._live = self._open, None, None
        if manager is not None:
            manager.__exit__(None, None, None)  # type: ignore[attr-defined]
