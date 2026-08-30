"""Platform adapter contract (PSM Batch 1 core §4.5).

This is the one file allowed in `adapters/` for this milestone slice: the
ABC and its supporting `FetchContext` so other core modules (and future
concrete adapters) can type against a stable interface. No concrete
adapter (Instagram, yt-dlp, gallery-dl) is implemented here.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Collection

from mfp.budget import FetchBudgetGovernor
from mfp.config import AppConfig
from mfp.download import ProgressCallback, download_manifest
from mfp.models import FetchResult, FetchResultBudget, Manifest
from mfp.policy import Policy, apply_policy_to_manifest


@dataclass
class FetchContext:
    """Carries everything an adapter needs beyond the URL/manifest itself.

    Adapters MUST route every outbound network action through
    `ctx.budget.acquire(platform)` -- a direct HTTP call that bypasses the
    governor is a review-blocking defect (PSM §4.5, as narrowed by the M4
    scope note: page endpoints, not CDN transfers).
    """

    budget: FetchBudgetGovernor
    config: AppConfig
    output_root: str
    #: Mutated by another thread to stop an in-flight transfer, which is why
    #: it is a field on a mutable dataclass rather than a value passed in:
    #: the queue worker holds this object while the HTTP layer flips it.
    cancel: bool = False
    #: Which item indices to transfer. None means all -- a different answer
    #: from an empty set, which means the caller selected nothing.
    select: Collection[int] | None = None
    #: Where transfer progress goes. The CLI prints it; the queue worker
    #: turns it into `TaskProgress` and an SSE frame; a test drops it.
    on_progress: ProgressCallback | None = None
    #: Take the picture without sound when the manifest names no audio track
    #: at all, instead of failing. Off unless a caller says otherwise: this
    #: is the answer to a refusal the user has already seen, not a setting
    #: that quietly changes what `fetch` means. Only the CLI's
    #: `--allow-silent-video` sets it today; the GUI has no toggle yet, so a
    #: queued task keeps the refusal.
    allow_silent_video: bool = False
    #: Which language's audio to take when a video publishes several (P-49).
    #:
    #: None means "the one it was recorded in", which is what anybody who
    #: does not think about this wants -- and what the picker got wrong
    #: before it could see languages at all. Setting it is how you ask for
    #: a dub on purpose; a video with one audio track ignores it.
    audio_language: str | None = None


class PlatformAdapter(ABC):
    """Two operations, deliberately separate (Phase 2 §2.1):
    `probe` only parses (cheap, no download); `fetch` acts on a
    previously-produced Manifest, so re-running with a different policy
    never requires re-probing.
    """

    name: str

    @abstractmethod
    def matches(self, url: str) -> bool:
        """True if this adapter handles `url`."""
        ...

    @abstractmethod
    def probe(self, url: str, ctx: FetchContext) -> Manifest:
        """Parse `url` into a Manifest without downloading media."""
        ...

    @abstractmethod
    def fetch(self, manifest: Manifest, policy: Policy, ctx: FetchContext) -> FetchResult:
        """Download the media described by `manifest`, applying `policy`."""
        ...


def standard_fetch(
    manifest: Manifest, policy: Policy, ctx: FetchContext, *, fallback_platform: str
) -> FetchResult:
    """The transfer half of `fetch()`, shared by every adapter that hands
    out direct media URLs.

    Two things happen here that `download.py` deliberately does not do
    itself, because both are adapter knowledge:

    * **ffmpeg presence decides what `best` means** (§14.1). Resolved once,
      up front, so a machine without it degrades to the best already-muxed
      rendition *and says so* -- rather than picking a video-only stream and
      discovering at transfer time that there is nothing to join it with.
    * **The budget block is read, not spent.** CDN transfers do not go
      through `ctx.budget.acquire()`; see `download.py`'s module docstring
      for why counting them would corrupt the pacing guard rather than
      merely slow it down.

    Shared rather than duplicated per adapter because the divergence would
    be silent: two copies of "resolve ffmpeg, apply policy, transfer" drift
    into one that forgets the §14.1 report, and nothing fails.
    """
    # `which` on BOTH branches, and that is the whole point: a configured
    # path used to be trusted just for being a non-empty string, so a
    # `binaries.ffmpeg` pointing at a file that does not exist reported
    # `mux_available=True`, let the policy pick a video-only rendition,
    # transferred all 9.8 MB of it, and only then died in the muxer with
    # `[WinError 2]` -- no file, no degradedReason, exit 1. Precisely the
    # outcome the paragraph above promises does not happen.
    #
    # `shutil.which` is the right primitive rather than `Path.exists()`: it
    # checks executability, resolves a bare name against PATH, and applies
    # PATHEXT, so a configured `...\bin\ffmpeg` with no extension still
    # resolves on Windows instead of being called missing.
    configured = ctx.config.binaries.ffmpeg
    ffmpeg = shutil.which(configured) if configured else shutil.which("ffmpeg")
    apply_policy_to_manifest(
        manifest,
        policy,
        mux_available=ffmpeg is not None,
        allow_silent_video=ctx.allow_silent_video,
    )

    platform = manifest.source.platform or fallback_platform
    snapshot = ctx.budget.snapshot(platform)
    return download_manifest(
        manifest,
        output_root=ctx.output_root,
        budget=FetchResultBudget(
            platform=platform,
            requests_used=snapshot.requests_used_hour,
            requests_remaining=snapshot.requests_remaining_hour,
            next_allowed_at=snapshot.next_allowed_at,
        ),
        ffmpeg=ffmpeg,
        allow_silent_video=ctx.allow_silent_video,
        select=ctx.select,
        on_progress=ctx.on_progress,
        cancel=(lambda: ctx.cancel),
    )
