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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from mfp.adapters.base import FetchContext, PlatformAdapter, standard_fetch
from mfp.adapters.instagram.chrome import Connector, Launcher
from mfp.adapters.instagram.sessions import ChromeSessions, PerCallChromeSessions
from mfp.adapters.instagram.extract import (
    _build_item,
    ExtractedPost,
    author_chain,
    dash_unavailable,
    extract_post_or_raise,
    iter_dash_manifests,
    looks_like_login_wall,
    outbound_links,
    post_metadata,
    reply_counts,
    segment_media_nodes,
)
from mfp.adapters import ytdlp
from mfp import logs
from mfp.download import build_client, measured_size
from mfp.errors import (
    BudgetExhausted,
    LoginWallError,
    MfpError,
    NoMediaInPost,
    RateLimitedError,
    UpstreamStructureChange,
)
from mfp.inputs import identify
from mfp.models import (
    ExcludedFormats,
    FetchResult,
    Manifest,
    ManifestSource,
    MediaItem,
    StrategyAttempt,
    ThreadSegment,
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


#: The one reason `_build_item` can hold a format back.
_CROP_REASON = "square_crop_not_rendition"


def _merge_excluded(
    excluded: list[ExcludedFormats], extra_crops: int
) -> list[ExcludedFormats]:
    """Add continuation crops to the post's own count, under one reason.

    Summed rather than appended: `Manifest.excluded` is a reason -> count
    report, and two rows saying `square_crop_not_rendition` would make a
    reader add them up to find out how many were dropped, or forget to.
    """
    if not extra_crops:
        return excluded
    merged = [row.model_copy() for row in excluded]
    for row in merged:
        if row.reason == _CROP_REASON:
            row.count += extra_crops
            return merged
    merged.append(ExcludedFormats(reason=_CROP_REASON, count=extra_crops))
    return merged


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
        #: The last rung's own verdict, kept so the terminus below cannot
        #: overwrite a cause that was determined with one that was not.
        last_error: MfpError | None = None

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
                last_error = exc
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

        # The terminus is a fallthrough, and a fallthrough may not assert a
        # cause (D-155, P-88). It knows one thing: every rung failed. When the
        # last rung -- the one that actually got the page, since the chain is
        # ordered cheapest to most capable -- already determined something
        # more specific than "the structure moved", that verdict is what the
        # caller gets. `no_media_in_post` is the case that made this matter:
        # raised by `_try_chrome`, it used to be swallowed here and reissued
        # as exit 5, so fixing the classifier alone would have changed
        # nothing a user could see.
        #
        # yt-dlp cannot win this on its own, and that is the point of taking
        # the LAST error rather than the most specific one: yt-dlp answering
        # "no media" about an Instagram post it could not parse is overridden
        # by whatever Chrome goes on to determine.
        if last_error is not None and last_error.error_code != UpstreamStructureChange.error_code:
            last_error.context.setdefault(
                "attempts", [attempt.model_dump() for attempt in attempts]
            )
            raise last_error
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
        no_media: NoMediaInPost | None = None
        try:
            post = extract_post_or_raise(html, shortcode=shortcode)
        except NoMediaInPost as exc:
            # The commonest segmented post is a text `1/n` whose pictures are
            # in the continuation. The classifier is right about the post the
            # URL names -- it has nothing -- and would be wrong about the
            # THREAD, so the question is asked again with the chain in view
            # before the answer is allowed out. `UpstreamStructureChange` is
            # deliberately not caught: a page whose structure moved cannot be
            # rescued by reading more of it.
            post = ExtractedPost(items=[], excluded=[])
            segments, extra_items, extra_crops = self._continuation(html, first_index=0)
            if not extra_items:
                no_media = exc
        else:
            segments, extra_items, extra_crops = self._continuation(
                html, first_index=len(post.items)
            )

        meta = post_metadata(html)
        stated, seen = reply_counts(html)
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
                segments=segments,
                replies_stated=stated,
                replies_seen=seen,
                links=outbound_links(html),
            ),
            items=post.items + extra_items,
            excluded=_merge_excluded(post.excluded, extra_crops),
        )
        if no_media is not None:
            # Still `no_media_in_post`, exit 3: for `fetch` and `probe` that is
            # the true answer, "nothing to download". But the post was READ,
            # and its words are the whole post -- so the manifest built from
            # them rides on the error for the one caller whose job is the
            # words (`brief`). Raising before building it is how a text-only
            # post became "nothing to explain" (2026-09-22).
            no_media.text_manifest = manifest
            raise no_media
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
    def _continuation(
        html: str, *, first_index: int
    ) -> tuple[list[ThreadSegment], list[MediaItem], int]:
        """The author's continuation chain, and the media hanging off it.

        Two rules hold this together and both were user rulings on
        2026-09-11. Only the AUTHOR's self-replies are collected, so nobody
        else's words or pictures enter the manifest. And continuation media
        IS collected (要抓續圖) -- but it arrives as ordinary `MediaItem`s
        carrying `segment_index`, so no existing consumer's idea of
        `Manifest.items` changes meaning behind its back: "the post's media"
        is still sayable as `segment_index == 0`.

        An ordinary post yields `([], [], 0)`. A one-post chain is not a
        chain, and reporting `segments` of length 1 for every Instagram photo
        would make the field noise that consumers learn to ignore.

        The third return value is the square-crop count, and it has to
        travel for the reason `_build_item` says it does: `Manifest.excluded`
        is what keeps a filtering decision from being a silent one. Dropping
        it here would make continuation pictures the one place in this
        adapter where formats are held back and nothing says so.
        """
        chain = author_chain(html)
        if len(chain) < 2:
            return [], [], 0

        segments: list[ThreadSegment] = []
        items: list[MediaItem] = []
        crops = 0
        index = first_index
        previous: int | None = None
        for position, node in enumerate(chain):
            built: list[MediaItem] = []
            if position > 0:
                # Segment 0's media is already in `post.items`, by the
                # ordinary path, with its excluded-format accounting. Rebuilding
                # it here would double every picture on the post.
                for child in segment_media_nodes(node):
                    item, dropped = _build_item(child, index)
                    crops += dropped
                    if item is None:
                        continue
                    item.segment_index = position
                    items.append(item)
                    built.append(item)
                    index += 1
            segments.append(
                ThreadSegment(
                    post_id=node.code,
                    index=position,
                    text=node.text,
                    timestamp=datetime.fromtimestamp(
                        node.taken_at, tz=timezone.utc
                    ).isoformat(),
                    gap_seconds=None if previous is None else node.taken_at - previous,
                    item_count=len(built),
                )
            )
            previous = node.taken_at

        # Segment 0's media went through the ordinary path, so its count is
        # exactly the number of items that were already there.
        segments[0].item_count = first_index
        return segments, items, crops

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
