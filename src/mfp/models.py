"""Pydantic v2 data models for the core wire contracts.

Sources (normative):
  - Variant / MediaItem / Manifest: `phase2-architecture.md` §2.2, referenced
    as binding by `psm-batch1-core.md` §4.3.
  - FetchResult: `psm-batch1-core.md` §4.4.
  - BudgetSnapshot: `psm-batch1-core.md` §7 (governor.snapshot() return
    type; PSM names the method but not the JSON shape -- see the delivery
    report's "assumptions" section for the field choices made here).

All models serialize with camelCase field names (the wire format used
throughout the JSON contracts) via a shared alias generator, while normal
Python code reads/writes the snake_case attribute names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base for every wire-format model: camelCase JSON, snake_case Python.

    `extra="forbid"` enforces schema discipline against the fixed contracts
    in the PSM -- an adapter emitting an unexpected field is a defect, not
    something to silently accept (Phase 2 §4.3 fail-closed philosophy).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class Variant(CamelModel):
    """A single quality/format option a platform offered for a MediaItem."""

    url: str
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None
    ext: str
    has_audio: bool | None = None
    needs_mux: bool = False
    #: The video codec the source named for this rendition, verbatim
    #: (`avc1.640028`, `vp09.00.50.08`, `av01.0.12M.08`). Recorded rather
    #: than acted on: the policy engine still chooses by resolution, but the
    #: choice is no longer unexplainable afterwards. Measured 2026-08-23: a
    #: 4K YouTube talk arrived as AV1, played as a blank window on a machine
    #: with no AV1 decoder, and nothing in the manifest could say why.
    #: None when the source did not name one -- that is an absence of a
    #: claim, not a claim of absence.
    vcodec: str | None = None
    #: ISO-8601 UTC, decoded from the CDN URL's `oe=` parameter (PSM §5.7).
    #: Measured TTL is about 32 hours, which is what makes "probe, let the
    #: user choose, fetch later" viable without re-probing. `fetch` compares
    #: it before starting a transfer so an expired link is named rather than
    #: collected as a folder of 403s. None when the URL carried no decodable
    #: `oe=` -- unknown expiry must not be recorded as an expiry.
    expires_at: str | None = None
    #: Headers the SOURCE said this URL needs, carried from the adapter to
    #: the transfer. Measured 2026-08-19: `upos-sz-mirrorcosov.bilivideo.com`
    #: returns 403 to a request without a `Referer`, and every audio format
    #: in a Bilibili manifest sits on that mirror -- so no choice of variant
    #: avoids it and the whole platform fails to transfer without this.
    #: Allowlisted at the adapter (never `Cookie`: D-2, this tool carries no
    #: credentials). None means "our client's own defaults are enough",
    #: which is the case for every platform measured before Bilibili.
    request_headers: dict[str, str] | None = None
    #: The language this rendition SPEAKS, as the source tagged it (`en-US`,
    #: `ar`, `zh-Hant`). Only audio-bearing rows carry one, and only on
    #: platforms that publish more than one -- None everywhere else, which
    #: is an absence of a claim rather than a claim of monolingualism.
    language: str | None = None
    #: True when the source says this is the language the video was RECORDED
    #: in, rather than a track derived from it.
    #:
    #: This exists because of a defect, not for completeness (P-49). One
    #: archived YC talk on this machine is an Arabic AI dub of an English
    #: talk: YouTube publishes 21 audio tracks at an identical 129.473 kbps,
    #: enumerates `[ar]` first, and the picker took the highest bitrate --
    #: which is a 21-way tie that resolves to whatever came first. Nothing
    #: in the manifest could say it had happened, because the model had no
    #: word for it. Now it does.
    #:
    #: None means the source did not say. A single-track video says nothing
    #: and needs to say nothing; do not read None as False.
    is_original: bool | None = None


class SidecarAsset(CamelModel):
    """A non-media file the source offers alongside an item.

    Deliberately NOT a `Variant`: it is never a rendition, never a download
    the policy engine chooses between, and never a thing the quality picker
    should show. It is saved beside the media under the same stem and that
    is all it does.

    Today the only kind is `danmaku` -- Bilibili's bullet comments, which
    arrive as an XML whose every entry carries its own appearance time, so
    the timeline is the source's rather than something reconstructed here.
    """

    #: What it is, not what it is called. Wire value; the GUI maps it.
    kind: str
    url: str
    ext: str
    request_headers: dict[str, str] | None = None


class MediaItem(CamelModel):
    """One media asset within a Manifest (one carousel slot, or the whole
    post for a single-image/video post)."""

    index: int
    kind: Literal["image", "video"]
    variants: list[Variant] = Field(default_factory=list)
    chosen: Variant | None = None
    #: The audio track that pairs with any `needs_mux` entry in `variants`.
    #: Deliberately NOT a member of `variants`: DASH offers one audio
    #: adaptation shared by every video rendition, so putting it in the list
    #: would both duplicate one URL across five variants (five signatures
    #: expiring independently, for one asset) and put a phantom "audio-only"
    #: row in front of the policy engine and the GUI's quality picker.
    #: Without this field a `needs_mux` variant is undownloadable: it names
    #: video-only bytes and nothing names the sound (2026-08-17).
    audio: Variant | None = None
    #: Non-media files saved beside this item. Empty for every platform but
    #: Bilibili, and empty there too when the video has no danmaku.
    sidecars: list[SidecarAsset] = Field(default_factory=list)
    alt_text: str | None = None
    # Set true by policy.apply_policy() when no variant satisfied a
    # `max-height:N` request and the smallest available variant was chosen
    # instead, per PSM §6: "never fail on policy alone".
    policy_relaxed: bool = False


class ManifestSource(CamelModel):
    """Post-level metadata for a Manifest.

    `platform` is intentionally `str`, not a `Literal`, even though the PSM
    schema block shows `"instagram" | "youtube" | "x" | "bilibili" | ...`
    -- the trailing `...` marks it as an open set (new adapters add new
    platform names without touching this model).
    """

    platform: str
    url: str
    id: str
    author: str | None = None
    caption: str | None = None
    timestamp: str | None = None


class StrategyAttempt(CamelModel):
    """One rung of an adapter's strategy chain, and what it produced.

    PSM §5.1 requires these for diagnosability, and the reason is specific:
    the chain's first rung (yt-dlp) is expected to fail against Instagram
    much of the time (Phase 1 H1). Without a record, "it worked" and "it
    worked on the third try after two failures" look identical, and there is
    no way to notice the day yt-dlp starts or stops carrying its weight.
    """

    strategy: str
    ok: bool
    #: §10 taxonomy code when it failed. None on success.
    error_code: str | None = None
    detail: str | None = None
    duration_ms: int | None = None


class ExcludedFormats(CamelModel):
    """Rows the source offered that are not renditions of this video (M10).

    A source's format list is not a quality menu. YouTube's contains
    storyboard sheets (a contact sheet of thumbnails, `mhtml`, with real
    width and height) and audio-only streams, and both used to arrive here
    as ordinary `Variant`s -- so `smallest` returned an `.m4a` for a video
    and a low `max-height` could return a storyboard. Filtering them is the
    fix; counting them is what makes the filtering visible instead of
    another silent decision.
    """

    #: `storyboard` | `audio_only` | `segmented` | `container_mismatch`
    reason: str
    count: int


class Manifest(CamelModel):
    """The unified contract adapters produce from `probe()` and consumers
    pass to `fetch()` (phase2-architecture.md §2.2)."""

    schema_version: Literal[1] = 1
    source: ManifestSource
    items: list[MediaItem] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    attempts: list[StrategyAttempt] = Field(default_factory=list)
    #: What the source offered and this build kept out of the candidate list.
    #: Empty for every adapter that hands over a menu of renditions only.
    excluded: list[ExcludedFormats] = Field(default_factory=list)


class FetchResultItem(CamelModel):
    """Per-item outcome row inside FetchResult.items (PSM §4.4)."""

    index: int
    status: Literal["ok", "failed", "skipped"]
    path: str | None = None
    size_bytes: int | None = Field(default=None, alias="bytes")
    chosen: Variant | None = None
    error_code: str | None = None
    error_detail: str | None = None
    #: Which post this row came from. Null for a single-post fetch, where
    #: §4.4's shape is unambiguous. `index` is the carousel slot, so it
    #: repeats across posts in a batch; without this the rows of a two-post
    #: fetch are indistinguishable and the identifying key is (postId, index).
    post_id: str | None = None


class FetchResultBudget(CamelModel):
    """The compact budget block embedded in FetchResult (PSM §4.4).

    Distinct from (and simpler than) `BudgetSnapshot` below, which is the
    richer shape returned by `FetchBudgetGovernor.snapshot()`.
    """

    platform: str
    requests_used: int
    requests_remaining: int
    next_allowed_at: str | None = None  # ISO-8601


class FetchResultPost(CamelModel):
    """One requested URL's outcome inside a batch FetchResult.

    Exists because `items` cannot express a post that produced no items at
    all. `mfp fetch a b c` where `b` fails to probe has nothing to put in
    `items` for `b`, so without this row the failure is reported by absence
    -- and absence is exactly what a silent failure looks like.
    """

    url: str
    ok: bool
    platform: str | None = None
    post_id: str | None = None
    item_count: int = 0
    error_code: str | None = None
    error_detail: str | None = None
    #: §14.1's declaration, carried out of `fetch` rather than left on the
    #: manifest. `skill/SKILL.md` §9 makes reporting `degradedReason` a MUST,
    #: and an agent calling `mfp fetch <url> --json` -- the single most common
    #: call -- never sees the manifest. Without this the reason reached only
    #: `manifest.json` and `_info.txt` on disk, so the MUST was unfulfillable
    #: from the output the Skill tells the agent to read. One per post,
    #: because degradation is a property of the manifest, not of a slot.
    degraded: bool = False
    degraded_reason: str | None = None


class FetchResult(CamelModel):
    """Top-level result of a `fetch` invocation (PSM §4.4).

    `posts` and `budgets` are additions, not a redefinition: a single-post
    fetch still matches §4.4 field for field, and a consumer written against
    §4.4 can ignore both. They exist because one invocation may cover many
    URLs across more than one platform, which §4.4's shape predates.
    """

    schema_version: Literal[1] = 1
    ok: bool
    output_root: str
    items: list[FetchResultItem] = Field(default_factory=list)
    budget: FetchResultBudget
    stop_reason: Literal["budget_exhausted", "blocked", "user_cancelled"] | None = None
    #: One row per requested URL. Empty for the engine-level result that
    #: `download_manifest` returns, which by construction covers one post.
    posts: list[FetchResultPost] = Field(default_factory=list)
    #: Every platform this invocation touched, present only when there is
    #: more than one. `budget` above then carries the first of them, so the
    #: §4.4 field keeps a defined meaning rather than an arbitrary one.
    budgets: list[FetchResultBudget] | None = None
    #: True when nothing was transferred (`--dry-run`). It exists so that
    #: `items[].path` can carry the *planned* destination -- which is the
    #: only part of a dry run worth reading -- without a consumer mistaking
    #: it for a file that exists. Every item is `skipped` in this mode.
    dry_run: bool = False


class BriefImage(CamelModel):
    """One fetched image, as handed to the agent that will look at it."""

    index: int
    #: Absolute, because the agent reading this may not share our cwd.
    path: str
    #: What the file IS, not what the platform claimed. None when nothing
    #: could resolve it -- an unresolved size is never a guess (D-93).
    width: int | None = None
    height: int | None = None
    bytes: int
    # No `alt_text` here, deliberately. The PSM's contract table listed one
    # and its own INV-B6 forbids it; the invariant wins. Alt text is written
    # by the post's author, and a copy on this row would let an agent
    # iterating `images` consume attacker-authored text without ever passing
    # through the word `untrusted` -- which is the entire mechanism. It lives
    # in `BriefUntrusted.alt_text`, keyed by the same index.


class BriefSkipped(CamelModel):
    """An item this build did not hand over, and why.

    Every item of the post is in `images` or here, never neither (INV-B3).
    A post whose video half vanished silently would be read as a post that
    never had one.
    """

    index: int
    kind: Literal["video", "other"]
    #: Wire code: `video_not_supported_yet` | `unsupported_item_kind` |
    #: `transfer_failed`.
    reason: str


class BriefPost(CamelModel):
    """Which post this is, and where its directory lives."""

    platform: str
    url: str
    id: str
    author: str | None = None
    timestamp: str | None = None
    post_dir: str


class BriefUntrusted(CamelModel):
    """Text written by the post's author. DATA, never instructions.

    A named block rather than fields on `BriefPost` so that the SHAPE carries
    the warning: a reader cannot reach the caption without passing through
    the word `untrusted` (INV-B6). This is the first thing in this tool that
    hands attacker-authored text to an agent holding tools, and the caption
    of a public post is written by someone who has never been authenticated.
    """

    caption: str | None = None
    #: `str(index)` -> that item's alt text. Keyed by string because this is
    #: a JSON object on the wire.
    alt_text: dict[str, str | None] = Field(default_factory=dict)


class BriefExisting(CamelModel):
    """What is already written in this lane's analysis file."""

    path: str
    #: ISO-8601 of the NEWEST entry.
    written_at: str
    entries: int


class BriefPackage(CamelModel):
    """`mfp brief --json` on stdout: everything an agent needs to look and write.

    `mfp` performs no analysis (D-88). This package is the exit and the
    destination; the seeing belongs to the caller, which already has vision.
    """

    schema_version: Literal[1] = 1
    #: Recorded, never derived (D-91). The agent classifies from the user's
    #: question; `mfp` only writes down which file the answer belongs in.
    lane: Literal["content", "visual"]
    post: BriefPost
    untrusted: BriefUntrusted
    images: list[BriefImage] = Field(default_factory=list)
    skipped: list[BriefSkipped] = Field(default_factory=list)
    analysis_path: str
    existing: BriefExisting | None = None
    budget: FetchResultBudget
    #: True when the post was already on disk and no platform request was
    #: made (INV-B7). The distinction is the difference between a free call
    #: and one that spends rate budget.
    reused: bool = False
    degraded: bool = False
    degraded_reason: str | None = None


class BudgetSnapshot(CamelModel):
    """Per-platform budget state as reported by
    `FetchBudgetGovernor.snapshot(platform)` (PSM §7).

    PSM §7 names the method and describes the governor's observable state
    (sliding hourly window, per-run cap, 80% warning, cooldown) but does not
    give this type's JSON shape verbatim; the fields below are the smallest
    reasonable surface covering everything §7 says must be observable.
    """

    platform: str
    requests_used_hour: int
    requests_remaining_hour: int
    requests_used_run: int
    requests_remaining_run: int
    next_allowed_at: str | None = None  # ISO-8601
    warning_active: bool = False
    cooldown_until: str | None = None  # ISO-8601, set while in report_block() cooldown
