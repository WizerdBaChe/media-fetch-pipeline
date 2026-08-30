"""Instagram / Threads adapter — the strategy chain (PSM §5.1).

Two rungs, tried in order, short-circuiting on success:

1. **`ytdlp`** — one subprocess, no browser. Expected to fail against
   Instagram much of the time (Phase 1 H1); kept first because failure is
   cheap and the day it starts working this path gets faster for free.
   Ruled by the user 2026-08-16 (Q1).
2. **`cdp-chrome`** — the real browser. This is the moat: the page is gated
   on TLS fingerprint, not on IP or User-Agent, so a real Chrome is the only
   thing that reliably gets the payload (Phase 1 §N4).

**There is no third rung.** PSM §5.5 pre-authorised an Open Graph fallback;
the user ruled it out on 2026-08-16 (Q2) and the reasoning is the one this
whole codebase is organised around: an og-fallback returns a preview-grade
image for the first carousel slot and calls it success. Handing over one
low-resolution thumbnail while reporting a completed probe is the same class
of failure as TRAP-4 — it looks like it worked. Failing is more useful.

Threads runs through this same adapter, not a second one: spike-02 §5
measured 8/8 successful Threads acquisitions on this exact path with no
Threads-specific handling.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx

from mfp.adapters.base import FetchContext, PlatformAdapter, standard_fetch
from mfp.adapters.instagram.chrome import Connector, Launcher
from mfp.adapters.instagram.sessions import ChromeSessions, PerCallChromeSessions
from mfp.adapters.instagram.extract import (
    dash_unavailable,
    extract_post_or_raise,
    iter_dash_manifests,
    looks_like_login_wall,
    post_metadata,
)
from mfp.adapters import ytdlp
from mfp import logs
from mfp.download import build_client, measured_size
from mfp.errors import (
    BudgetExhausted,
    LoginWallError,
    MfpError,
    RateLimitedError,
    UpstreamStructureChange,
)
from mfp.inputs import identify
from mfp.models import (
    FetchResult,
    Manifest,
    ManifestSource,
    StrategyAttempt,
)
from mfp.policy import Policy

#: Platforms this adapter serves. Threads is a variant, not a second adapter.
PLATFORMS: frozenset[str] = frozenset({"instagram", "threads"})

#: How many image headers to read at once when `stp` did not answer, and how
#: long the whole repair may take per image item before the rest are left
#: unresolved. Four because these are small ranged GETs to one CDN and the
#: point is to stop a drifted grammar from turning `probe` into a stall, not
#: to go fast; ten seconds because an unresolved size is a survivable
#: degradation and a probe that looks hung is not.
SIZE_WORKERS = 4
SIZE_BUDGET_SECONDS = 10.0

def _now_ms(clock: Callable[[], float]) -> int:
    return int(clock() * 1000)


class InstagramAdapter(PlatformAdapter):
    name = "instagram"

    def __init__(
        self,
        *,
        state_dir: Path,
        connector: Connector,
        http_get: Callable[[str], str],
        launcher: Launcher | None = None,
        ytdlp_probe: Callable[..., Manifest] = ytdlp.probe,
        clock: Callable[[], float] = time.monotonic,
        sessions: ChromeSessions | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._connector = connector
        self._http_get = http_get
        self._launcher = launcher
        self._ytdlp_probe = ytdlp_probe
        self._clock = clock
        #: Who owns the browser's lifetime. Per-call by default, which is
        #: the behaviour that shipped and the right one for a single URL;
        #: the queue worker passes a shared session so a batch pays the
        #: 4-6 s launch once instead of once per task.
        self._sessions = sessions or PerCallChromeSessions(
            state_dir=state_dir,
            connector=connector,
            http_get=http_get,
            launcher=launcher or _default_launcher,
        )

    def matches(self, url: str) -> bool:
        """Delegates to the same identifier the paste box uses.

        Sharing it is the point: a URL the input pipeline accepted and this
        adapter rejects would be a task that can never leave PARSED, and the
        user would have no way to see why.
        """
        from urllib.parse import urlsplit

        split = urlsplit(url)
        identified = identify(split.hostname or "", split.path, {})
        return identified is not None and identified[0] in PLATFORMS

    # --- probe ---------------------------------------------------------------

    def probe(self, url: str, ctx: FetchContext) -> Manifest:
        """Walk the strategy chain, recording every rung.

        Each rung that touches the network takes its own budget slot — both
        of them do, so a probe that falls through to Chrome costs two. That
        is correct and deliberate: two requests went out. Undercounting here
        would let the governor's hourly window drift away from reality, and
        the window is the only thing standing between this tool and the rate
        limit it exists to stay under.
        """
        platform = self._platform_of(url)
        attempts: list[StrategyAttempt] = []

        for strategy in (self._try_ytdlp, self._try_chrome):
            started = _now_ms(self._clock)
            try:
                ctx.budget.acquire(platform)
            except BudgetExhausted:
                # Not a strategy failure: there is no point trying the next
                # rung either, and the caller needs the real reason.
                raise

            try:
                manifest = strategy(url, platform, ctx)
            except MfpError as exc:
                attempts.append(
                    StrategyAttempt(
                        strategy=strategy.__name__.removeprefix("_try_"),
                        ok=False,
                        error_code=exc.error_code,
                        detail=str(exc)[:300],
                        duration_ms=_now_ms(self._clock) - started,
                    )
                )
                if isinstance(exc, (LoginWallError, RateLimitedError)):
                    # A block signal ends the chain. Retrying through another
                    # rung is more traffic at exactly the wrong moment, and
                    # PSM §5.1 maps this to a different exit code than a
                    # structure change for the same reason.
                    exc.context.setdefault("attempts", [a.model_dump() for a in attempts])
                    raise
                continue
            except ytdlp.YtDlpUnavailable as exc:
                attempts.append(
                    StrategyAttempt(
                        strategy="ytdlp",
                        ok=False,
                        error_code=None,
                        detail=str(exc)[:300],
                        duration_ms=_now_ms(self._clock) - started,
                    )
                )
                continue

            attempts.append(
                StrategyAttempt(
                    strategy=strategy.__name__.removeprefix("_try_"),
                    ok=True,
                    duration_ms=_now_ms(self._clock) - started,
                )
            )
            manifest.attempts = attempts
            return manifest

        raise UpstreamStructureChange(
            f"every strategy failed for {url}. No Open Graph fallback exists by "
            "ruling (Q2, 2026-08-16): a preview-grade thumbnail reported as "
            "success is worse than a failure you can act on.",
            url=url,
            attempts=[attempt.model_dump() for attempt in attempts],
        )

    def _try_ytdlp(self, url: str, platform: str, ctx: FetchContext) -> Manifest:
        return self._ytdlp_probe(
            url, platform=platform, executable=ctx.config.binaries.yt_dlp
        )

    def _try_chrome(self, url: str, platform: str, ctx: FetchContext) -> Manifest:
        with self._sessions.session(ctx.config) as session:
            html = session.fetch_html(url)

        self._reject_login_wall(html, url)
        shortcode = self._shortcode_of(url)
        post = extract_post_or_raise(html, shortcode=shortcode)

        meta = post_metadata(html)
        manifest = Manifest(
            source=ManifestSource(
                platform=platform,
                url=url,
                # The post's own code wins over the URL's. They differ for a
                # Threads share link, and taking the URL's would file one
                # post under two names depending on how it was reached.
                id=meta.code or shortcode,
                author=meta.author,
                caption=meta.caption,
                timestamp=meta.timestamp,
            ),
            items=post.items,
            excluded=post.excluded,
        )
        self._note_unreachable_quality(manifest, html)
        self._resolve_unknown_image_sizes(manifest)
        return manifest

    # --- fetch ---------------------------------------------------------------

    def fetch(self, manifest: Manifest, policy: Policy, ctx: FetchContext) -> FetchResult:
        """Apply `policy` to a probed Manifest and transfer the result.

        Nothing here is Instagram-specific: the page is what needs a real
        browser, the CDN is plain HTTPS. `standard_fetch` carries the §14.1
        degradation report and the read-don't-spend budget rule, both of
        which are adapter knowledge rather than transfer knowledge.
        """
        return standard_fetch(manifest, policy, ctx, fallback_platform=self.name)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _platform_of(url: str) -> str:
        from urllib.parse import urlsplit

        split = urlsplit(url)
        identified = identify(split.hostname or "", split.path, {})
        return identified[0] if identified else "instagram"

    @staticmethod
    def _shortcode_of(url: str) -> str:
        from urllib.parse import urlsplit

        split = urlsplit(url)
        identified = identify(split.hostname or "", split.path, {})
        return identified[1] if identified else url

    @staticmethod
    def _reject_login_wall(html: str, url: str) -> None:
        """Separate "they want us logged in" from "the payload moved".

        Both produce zero media, and conflating them sends the reader to
        rewrite the extractor when the real answer is that the logged-out
        path — the entire premise of D-2 — has stopped working. Spike-02
        measured 17/17 with no login wall, so the first occurrence of this is
        news worth stopping for.
        """
        if looks_like_login_wall(html):
            raise LoginWallError(
                f"{url} served a login wall. D-2 forbids logging in, so this is a "
                "stop signal: the logged-out path that spike-02 measured at 17/17 "
                "no longer works for this post.",
                url=url,
            )

    @staticmethod
    def _resolve_unknown_image_sizes(manifest: Manifest) -> None:
        """Fill in sizes `stp` could not give, by reading the image's header.

        The ruling (ADR-B1) is that `probe` must finish knowing every
        candidate's size, so that `--policy` works on Instagram photos
        everywhere and the cost is paid once per post rather than per fetch.
        What the ruling assumed it would cost -- roughly 42 small round-trips
        on a seven-image carousel -- is what `measured=` counts below, and on
        the measured payload shape it counts ZERO: `stp` already answered
        (D-93). This runs for the rows it did not.

        These are CDN requests, so they are outside the governor by
        definition (D-34): a probe costs rate budget, a transfer does not.

        **Bounded on both axes, because the bad case is the whole point.**
        When the grammar has NOT moved this loop does nothing. When it has,
        every row of every item lands here at once -- and serial requests on
        the transfer client's 60-second read timeout would leave `probe`
        blocking for minutes with nothing on screen. So: four at a time, and
        a deadline of `SIZE_BUDGET_SECONDS` per image item, after which the
        rest are left unresolved and SAID to be. An unknown size is a
        degradation `brief` reports; a probe that appears to have hung is a
        bug report.

        Never fatal. A size that stays unknown is reported as unknown.
        """
        unresolved = [
            variant
            for item in manifest.items
            if item.kind == "image"
            for variant in item.variants
            if variant.width is None or variant.height is None
        ]
        image_items = sum(1 for item in manifest.items if item.kind == "image")
        static = (
            sum(len(item.variants) for item in manifest.items if item.kind == "image")
            - len(unresolved)
        )
        if not unresolved:
            logs.annotate(imageSizes=f"static={static} measured=0 unresolved=0")
            return

        deadline = time.monotonic() + SIZE_BUDGET_SECONDS * max(1, image_items)
        measured = 0
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

        def resolve(variant) -> bool:
            if time.monotonic() > deadline:
                return False
            size = measured_size(
                variant.url, client=client, headers=variant.request_headers
            )
            if size is None:
                return False
            variant.width, variant.height = size
            return True

        with build_client() as client:
            client.timeout = timeout
            with ThreadPoolExecutor(max_workers=SIZE_WORKERS) as pool:
                measured = sum(pool.map(resolve, unresolved))

        remaining = len(unresolved) - measured
        logs.annotate(
            imageSizes=f"static={static} measured={measured} unresolved={remaining}"
        )
        if measured or remaining:
            # Only when something other than the free path happened. `annotate`
            # writes to the action log, not the terminal, so without this line
            # a drifted token grammar is invisible to the person watching --
            # and that is exactly the case worth seeing.
            print(
                f"image sizes: {static} from the URL, {measured} measured, "
                f"{remaining} unresolved",
                file=sys.stderr,
            )

    @staticmethod
    def _note_unreachable_quality(manifest: Manifest, html: str) -> None:
        """Say when a higher quality exists but cannot be fetched.

        `dash_unavailable` reports segmented representations, which the
        transfer layer cannot pull with a ranged GET. Left unsaid, `best`
        would quietly return the highest *reachable* variant and call it
        best — the silent downgrade §14.1 forbids.
        """
        if not any(item.kind == "video" for item in manifest.items):
            return
        # Through the payload walk, not a scan of the raw text for the key: a
        # hand-rolled scan has to guess where the JSON string ends, and it
        # guesses wrong the moment the surrounding shape changes -- which is
        # the one thing this page is guaranteed to do.
        skipped = [
            representation
            for raw in iter_dash_manifests(html)
            for representation in dash_unavailable(raw)
        ]
        if not skipped:
            return
        best_skipped = max(skipped, key=lambda representation: representation.height or 0)
        manifest.degraded = True
        manifest.degraded_reason = (
            f"a {best_skipped.height}p rendition exists but is delivered as a "
            "segment sequence, which this build cannot assemble; the highest "
            "single-file rendition was offered instead"
        )


def _default_launcher(argv: list[str]):
    import subprocess

    # argv-array invocation only -- never a shell string (Phase 2 §4.2).
    # stdin=DEVNULL for the same reason as `capture._default_launcher`.
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, shell=False)


__all__ = ["PLATFORMS", "InstagramAdapter"]
