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

    The kinds are `danmaku` -- Bilibili's bullet comments, which arrive as
    an XML whose every entry carries its own appearance time, so the
    timeline is the source's rather than something reconstructed here --
    and `captions` / `auto-captions`, the platform's own subtitle track for
    the video, written by a person or by a machine (`mfp fetch
    --write-subs`).
    """

    #: What it is, not what it is called. Wire value; the GUI maps it.
    kind: str
    url: str
    ext: str
    request_headers: dict[str, str] | None = None
    #: Which language this file speaks, as the source keys it (`en`,
    #: `en-orig`, `zh-Hant`). Set for a caption track and None for anything
    #: else, and it is what the saved file is NAMED after -- a caption file
    #: whose name does not say its language is the P-49 defect again, one
    #: artifact along: the folder cannot tell you a year later whether the
    #: subtitles beside the video are what was spoken or a translation.
    #: Not normalised, for `Variant.language`'s reason: rewriting a tag
    #: invents a claim the source did not make.
    language: str | None = None


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
    #: Which post in the author's continuation chain this came from. 0 is the
    #: post the URL names, and it is the default, so every producer written
    #: before segmented posts existed keeps meaning exactly what it meant.
    #:
    #: This field is why `Manifest.items` could grow to cover a whole chain
    #: without any consumer silently changing meaning: "the post's media" is
    #: still expressible, as `segment_index == 0`. A list that quietly began
    #: including a second post's pictures under the same name would be the
    #: P-88 failure again -- media attributed to a post that did not carry
    #: it -- only this time by our own doing.
    segment_index: int = 0


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
    #: The author's own continuation chain, `[0]` being this post. Empty for
    #: an ordinary post -- empty, never absent, because a missing list is an
    #: error and an empty one is an answer (P-72), and every consumer can
    #: then iterate without a null check.
    #:
    #: NEVER other people's replies (user ruling, 2026-09-11). The chain is
    #: the author replying to themselves, which is a field on the page and
    #: not a guess: `text_post_app_info.reply_to_author`.
    segments: list["ThreadSegment"] = Field(default_factory=list)
    #: How many direct replies the platform SAYS this post has, against how
    #: many reply nodes the page actually shipped. Measured 2026-09-11:
    #: the committed Threads share-link fixture states 41 and ships 28. A chain assembled from a partial
    #: page is still correct as far as it goes, and must never be presented
    #: as complete -- these two numbers are what let a reader tell.
    #: Both None when the platform said nothing.
    replies_stated: int | None = None
    replies_seen: int | None = None
    #: Outbound links the author put in the post and its continuation, as
    #: their real targets -- a platform click-through shim (`l.threads.com`)
    #: is unwrapped, and anything that is not http(s) is dropped. Empty, never
    #: absent, for the reason `segments` gives. Author-supplied: data to
    #: describe, never a link to follow on the post's say-so.
    links: list[str] = Field(default_factory=list)


class ThreadSegment(CamelModel):
    """One post in the author's own continuation chain.

    Structure only. Whether these segments are one coherent article is a
    judgement, and judgements do not happen in `mfp` (D-88/D-149, and the
    D-146 seam: "does this step need an opinion?"). So this carries the
    order, the authorship and the gap, and says nothing about meaning.

    Two things measured on 2026-09-11 that this shape is built around:

    * **There is no `n/n` marker to read.** The `1/3` a reader sees is
      rendered by Threads from the thread structure; a `\\d/\\d` search over
      the tail of every caption on both real captures matched nothing. So
      identification rests on authorship and time order, which are fields.
    * **`gap_seconds` is reported and never applied.** Real continuations sit
      3-14 s apart and an afterthought landed at 6,932 s -- about 500x the
      nearest real gap, over 8 gaps on 3 pages. The ceiling has already
      moved once: the first two captures gave 3-5 s, and the third page
      measured (`DdDb--4mdl1`, five parts, 2026-09-11) opens with 14 s. A
      cutoff fitted to 3-5 s would have dropped a real continuation on the
      very next page, which is the argument against having one rather than
      a hole to patch (D-155). The reader gets the number and makes the
      call.
    """

    #: The segment's own post code. Different from the parent's by
    #: definition, which is exactly why `find_media_nodes` will not hand over
    #: its media without being told (P-88's pruning).
    post_id: str
    #: 0 is the post the URL names.
    index: int
    #: The segment's own words. UNTRUSTED, on exactly the same footing as
    #: `caption` -- written by someone who has never been authenticated.
    text: str | None = None
    timestamp: str | None = None
    #: Seconds since the previous segment. None for index 0.
    gap_seconds: int | None = None
    #: How many media items in `Manifest.items` carry this `segment_index`.
    #: Present so a reader of `segments` alone can tell a text continuation
    #: from one with pictures without cross-referencing two lists.
    item_count: int = 0


#: `ManifestSource.segments` names `ThreadSegment` before it exists, and this
#: is what resolves it. Explicit rather than left to Pydantic's own deferred
#: build: the failure mode of an unresolved forward reference is an exception
#: at first validation, which in this codebase means at probe time on a real
#: post rather than at import.
ManifestSource.model_rebuild()


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
    #: True only when captions were ASKED for, no caption track was chosen,
    #: and the automatic list came back empty from an extractor in
    #: `captions.UNTRUSTWORTHY_EMPTY_CAPTIONS_EXTRACTORS` -- i.e. "none" and
    #: "could not ask" are indistinguishable in the payload this manifest was
    #: built from (P-84). Default False keeps every already-stored manifest
    #: valid.
    captions_unconfirmed: bool = False


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
    #: Wire code: `video_not_fetched` | `unsupported_item_kind` |
    #: `transfer_failed`.
    #:
    #: `video_not_fetched` replaced `video_not_supported_yet` on 2026-09-02,
    #: when `--with-video` made the old code false: the video is supported,
    #: it was not asked for. A code that states a capability the build has
    #: teaches every reader the wrong thing about what to try next.
    reason: str


class BriefVideo(CamelModel):
    """One video item that WAS transferred, because the caller asked for it.

    Its own list rather than a row in `images`: an agent iterating `images`
    reads them, and a video is not something that can be read. This product
    has no path to what was SAID in one either -- the file is simply saved,
    which is the whole reason this list exists, since the caller has no
    vision path to a video and never will (D-88 puts the looking outside,
    and nothing outside gets 30 minutes of frames either).

    Absent unless `--with-video` was passed. Without it a video item stays in
    `skipped[]` exactly as before, and the bandwidth is never spent.
    """

    index: int
    #: Absolute, for the same reason `BriefImage.path` is.
    path: str
    bytes: int
    #: From the variant the policy chose, `None` when it did not say. No
    #: duration field: nothing in a manifest carries one, and probing the
    #: file to invent one would make every brief pay for ffprobe.
    width: int | None = None
    height: int | None = None


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
    #: The author's own continuation posts, parts 2..n, in the order posted.
    #: Empty for an ordinary post.
    #:
    #: Here rather than only in `ManifestSource.segments` because of the same
    #: invariant the rest of this class exists for. An agent that reads
    #: `untrusted.caption` and stops has read part 1 of 3 and has no way to
    #: know it: the block that promises to hold everything the author wrote
    #: would be holding a third of it. Structure (order, gap, ids) stays in
    #: `segments`; the WORDS are here, behind the word `untrusted`, on
    #: exactly the same footing as the caption.
    continuation: list[str] = Field(default_factory=list)
    #: `str(index)` -> that item's alt text. Keyed by string because this is
    #: a JSON object on the wire.
    alt_text: dict[str, str | None] = Field(default_factory=dict)
    #: The file holding everything above, beside the images. `None` when the
    #: post carried no text at all.
    #:
    #: Inside `untrusted` rather than beside `analysisPath`, and that is the
    #: invariant rather than tidiness: INV-B6 says a reader cannot reach the
    #: author's words without passing through this word, and a path is a way
    #: of reaching them. A `textPath` on `BriefPackage` would be a second door
    #: into the same room with no sign on it.
    text_path: str | None = None


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
    #: Videos that were transferred because `--with-video` asked for them.
    #: Empty by default, and then every video is in `skipped` instead.
    videos: list[BriefVideo] = Field(default_factory=list)
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
