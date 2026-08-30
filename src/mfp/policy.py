"""Quality policy engine (PSM Batch 1 core §6).

The selection semantic (`choose`) lives in exactly one place; only the
yt-dlp format-selector bridge differs per platform (Phase 2 §2.3:
"semantics over implementation -- the policy semantic lives in one place,
the bridge differs per platform, not the semantics").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Union

from mfp.models import ExcludedFormats, Manifest, MediaItem, Variant

#: PSM §14.1 fixes this exact token. It is a code, not prose, because the GUI
#: and the Skill both branch on it.
MUX_UNAVAILABLE_REASON = "ffmpeg_missing_progressive_only"

#: The other way a `needs_mux` variant turns out to be unusable: this build
#: has ffmpeg but the manifest records no audio track to join it with.
#:
#: Not in §14.1, which only anticipated a missing muxer. It became reachable
#: when M8 gave the yt-dlp platforms a `fetch` path: `manifest_from_dump`
#: marks video-only formats `needsMux` from yt-dlp's own codec fields but
#: never records a second URL, so `best` would otherwise choose bytes this
#: build cannot complete. An Instagram post whose DASH audio adaptation
#: moved lands here too -- and that used to surface as a transfer failure
#: after the download had already started.
NO_AUDIO_TRACK_REASON = "no_audio_track_progressive_only"

#: The third outcome, and the only one that hands over a file with no sound:
#: the manifest names no audio track anywhere, every rendition is video-only,
#: and the caller asked for the picture anyway (`allow_silent_video`).
#:
#: Opt-in, never a default, and never reached when the sound merely cannot be
#: JOINED -- see `_selectable`. A run that did not ask for this still gets the
#: refusal, which is what keeps §14.1 intact: the defect §14.1 names is being
#: handed something weaker than you asked for without being told, and a flag
#: you typed yourself is being told.
SILENT_VIDEO_REASON = "no_audio_track_video_only"

#: Containers that cannot carry a picture. A row in one of these is a sound
#: track whatever else it claims, so it is never a rendition of a MediaItem
#: -- neither of a video nor of an image.
#:
#: Measured 2026-08-19 on Instagram Reel `DbYNRLzjgiF`: the manifest offered
#: twelve rows -- eleven video-only renditions up to 1080x1920 and one
#: `.m4a` -- and `fetch --policy best` returned the `.m4a`, 519 KB of bare
#: AAC, as `status: "ok"`. Nothing downstream was lying: `withholding_reason`
#: correctly reported that the video-only rows had no track to be joined to,
#: and `_selectable` correctly fell back to what was left. What was left was
#: a sound file, and no field on it said "this has no picture" -- the one row
#: claiming `hasAudio` is exactly what that fallback reaches for.
#:
#: `mp4` and `webm` are deliberately absent, and so is any test on
#: width/height. Both containers carry video, so an audio stream in one is
#: indistinguishable from a rendition whose dimensions the source did not
#: report -- and those are real: the Instagram capture in
#: `tests/fixtures/instagram` has three progressive `mp4` variants with
#: `hasAudio=True` and no width or height at all, and they are the only rows
#: a machine without ffmpeg can use. A gate may only rule on what it can
#: determine; withholding those would trade this defect for a worse one.
AUDIO_ONLY_EXTS: frozenset[str] = frozenset(
    {"m4a", "mp3", "aac", "opus", "oga", "ogg", "weba", "wav", "flac"}
)


@dataclass(frozen=True)
class MaxHeight:
    """Policy variant: choose the best quality with height <= `value`."""

    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError("MaxHeight.value must be a positive integer")


Policy = Union[Literal["best"], Literal["smallest"], MaxHeight]

# CLI/config policy string grammar (PSM §4.1): best | max-height:<N> | smallest
_MAX_HEIGHT_STRING_RE = re.compile(r"^max-height:(\d+)$")


def parse_policy(raw: str) -> Policy:
    """Parse the CLI/config policy string form into a `Policy` value."""
    if raw == "best":
        return "best"
    if raw == "smallest":
        return "smallest"
    match = _MAX_HEIGHT_STRING_RE.match(raw)
    if match:
        return MaxHeight(int(match.group(1)))
    raise ValueError(f"unrecognized policy string: {raw!r}")


def _sort_key(variant: Variant) -> tuple[int, int, int]:
    """`(height or 0, bitrate or 0, width or 0)` per PSM §6.

    Unchanged by the `max-height` fix below, and deliberately: within one
    post every rendition shares an aspect ratio, so ordering by height ranks
    them correctly whichever way up the video is. Only the *threshold*
    needed fixing, not the ordering.
    """
    return (variant.height or 0, variant.bitrate or 0, variant.width or 0)


def resolution_class(variant: Variant) -> int:
    """The number a person means when they say "1080p": the **short side**.

    Ruled by the user 2026-08-17, after the first live fetch measured a Reel
    at 1080x1920. Read literally, `max-height:1080` compares against
    `height` -- 1920 -- and therefore excludes the best rendition of
    essentially every Reel, silently handing back a smaller one. Nobody
    asking for 1080p means "not that 1080x1920 video".

    Landscape is unaffected: for 1920x1080 the short side *is* the height.
    A variant with no width falls back to height, which is the best
    available answer rather than a guess.
    """
    if variant.width and variant.height:
        return min(variant.width, variant.height)
    return variant.height or 0


def _smallest(variants: list[Variant]) -> Variant:
    """The smallest rendition, preferring one that declares a size.

    A variant with no width and no height sorts to the bottom of `_sort_key`
    -- `(0, 0, 0)` -- so `smallest` returned it ahead of every measured
    rendition. Measured 2026-08-19 on an Instagram Reel: the row set carries
    three mp4s with no dimensions at all beside eight that declare 720x1280
    or 1080x1920, and `smallest` took one of the three. Missing dimensions
    are not a claim to be small; they are the absence of a claim, and
    answering "smallest" with an unknown answers a question nobody asked.

    When nothing declares a size the unsized rows are all there is, and the
    old behaviour is the only behaviour available.
    """
    sized = [variant for variant in variants if resolution_class(variant)]
    return min(sized or variants, key=_sort_key)


def _choose_core(variants: list[Variant], policy: Policy) -> tuple[Variant | None, bool]:
    """Core selection logic. Returns `(chosen, policy_relaxed)`.

    `policy_relaxed` is true only for the max-height relaxation path: when
    no variant satisfies `height <= N`, the smallest available variant is
    chosen instead of failing (PSM §6: "never fail on policy alone").
    """
    if not variants:
        return None, False

    if policy == "best":
        return max(variants, key=_sort_key), False

    if policy == "smallest":
        return _smallest(variants), False

    if isinstance(policy, MaxHeight):
        qualifying = [v for v in variants if resolution_class(v) <= policy.value]
        if qualifying:
            return max(qualifying, key=_sort_key), False
        return _smallest(variants), True

    raise TypeError(f"unsupported policy: {policy!r}")


def choose(variants: list[Variant], policy: Policy) -> Variant | None:
    """Select a Variant per policy (PSM §6 signature).

    This returns only the chosen Variant. Use `apply_policy()` when you
    also need `MediaItem.policyRelaxed` recorded for the relaxation path.
    """
    chosen, _relaxed = _choose_core(variants, policy)
    return chosen


#: The human half of each withholding reason. Kept beside the codes so a new
#: reason cannot be added without someone having to write what it means.
_WITHHELD_DETAIL: dict[str, str] = {
    MUX_UNAVAILABLE_REASON: (
        "ffmpeg is absent, so the {height}p rendition -- video-only bytes needing a "
        "muxer -- was withheld"
    ),
    NO_AUDIO_TRACK_REASON: (
        "the manifest records no audio track, so the {height}p rendition -- video-only "
        "bytes with nothing to join them to -- was withheld"
    ),
}

#: How that sentence ends, which is not a property of the reason: the same
#: cause leaves a lower rung to fall back to on one post and nothing at all on
#: the next. Promising "the best already-muxed rendition instead" when `chosen`
#: is None describes a file the user is not getting -- the §14.1 defect
#: committed by the sentence written to report it.
_FELL_BACK_TAIL = " and the best already-muxed rendition offered instead"
_NOTHING_LEFT_TAIL = ", and no other rendition of it can be completed here"

#: The silent-video declaration. Its own sentence rather than a third tail,
#: because it reports the opposite shape: nothing was withheld, and the file
#: that WAS handed over is the incomplete one.
_SILENT_VIDEO_DETAIL = (
    "the manifest records no audio track and no rendition of this post carries one, so "
    "the {height}p rendition was delivered WITHOUT SOUND, which this run allowed"
)


def _record_exclusion(manifest: Manifest, reason: str, count: int) -> None:
    """Add to the manifest's ledger of rows that never reached the menu.

    Counting them is what keeps `_renditions` from being one more silent
    decision -- the same argument D-69 made for the adapter's own filter, and
    the GUI already turns this field into a `formats_excluded` notice.

    Merged into the adapter's row for the same reason rather than appended
    beside it: two `audio_only` entries read as two separate findings.
    """
    for entry in manifest.excluded:
        if entry.reason == reason:
            entry.count += count
            return
    manifest.excluded.append(ExcludedFormats(reason=reason, count=count))


def withholding_reason(item: MediaItem, *, mux_available: bool) -> str | None:
    """Why this item's `needs_mux` variants cannot be used, or None.

    A `needs_mux` variant is video-only bytes plus a separate audio track.
    Two things have to be true to finish one, and each can be false on its
    own: a muxer must exist, and the manifest must actually name the second
    stream. Offering a variant when either is missing is the §14.1 silent
    downgrade in its purest form -- the user asks for `best`, is told they
    got it, and receives a file with no sound (or, once M4 landed, a
    transfer that fails partway with the bytes already spent).

    Asked in this order on purpose. When the manifest names no second
    stream, installing ffmpeg changes nothing, so reporting the muxer first
    -- which is what this did until 2026-08-20 -- sends the reader to a fix
    that cannot work. The muxer only becomes the answer once there is
    something to mux.
    """
    if item.audio is None:
        return NO_AUDIO_TRACK_REASON
    if not mux_available:
        return MUX_UNAVAILABLE_REASON
    return None


def _renditions(item: MediaItem) -> list[Variant]:
    """`item.variants` minus the rows that are not renditions of it.

    `variants` is a quality menu, and the sound track of a video is not an
    entry on it -- that is what `MediaItem.audio` is for, and why the yt-dlp
    adapter keeps every audio row out of `variants` at translation time
    (D-69). A row in an audio-only container that reaches this far arrived by
    some other route, and the last place able to refuse it is the one that
    does the choosing.

    Applies to both kinds because neither is audio: an `.m4a` is no more a
    rendition of an image than of a video. If `MediaItem.kind` ever gains an
    `audio` member, this is one of the places that has to learn about it.
    """
    return [variant for variant in item.variants if variant.ext.lower() not in AUDIO_ONLY_EXTS]


def _selectable(
    item: MediaItem, *, mux_available: bool, allow_silent_video: bool = False
) -> list[Variant]:
    """The variants this build can actually turn into a playable file.

    `allow_silent_video` opens one extra door, and only one: when the
    manifest names no audio track at all AND nothing complete survives, the
    video-only renditions become selectable and are delivered without sound
    (`SILENT_VIDEO_REASON`). It is deliberately no help when the sound
    EXISTS and only the muxer is missing -- there the file is one ffmpeg
    install away, and shipping a silent copy would spend the user's bytes
    discarding something recoverable. Missing sound and unjoinable sound
    look alike in the output and are opposites in what to do about them.
    """
    candidates = _renditions(item)
    if withholding_reason(item, mux_available=mux_available) is None:
        return candidates
    complete = [variant for variant in candidates if not variant.needs_mux]
    if complete or not allow_silent_video or item.audio is not None:
        return complete
    return candidates


def apply_policy(
    item: MediaItem,
    policy: Policy,
    *,
    mux_available: bool = True,
    allow_silent_video: bool = False,
) -> MediaItem:
    """Apply `policy` to `item.variants`, setting `item.chosen` and
    `item.policy_relaxed` in place. Returns `item` for chaining.

    Variants that cannot be completed are withheld first (see
    `withholding_reason`). If that leaves nothing, `chosen` is None: an item
    whose only renditions need a muxer this machine does not have is
    genuinely undownloadable, and saying so is `dependency_missing`, not a
    policy relaxation. `allow_silent_video` is the caller's answer to one
    branch of that ending -- see `_selectable`.
    """
    chosen, relaxed = _choose_core(
        _selectable(item, mux_available=mux_available, allow_silent_video=allow_silent_video),
        policy,
    )
    item.chosen = chosen
    item.policy_relaxed = relaxed
    return item


def _declare(manifest: Manifest, detail: str) -> None:
    """Record one degradation on the manifest.

    Appended rather than assigned: a manifest can already be degraded for an
    unrelated reason (a segmented rendition), and overwriting that would
    trade one silent downgrade for another.
    """
    manifest.degraded_reason = (
        detail if not manifest.degraded_reason else f"{manifest.degraded_reason}; {detail}"
    )
    manifest.degraded = True


def apply_policy_to_manifest(
    manifest: Manifest,
    policy: Policy,
    *,
    mux_available: bool = True,
    allow_silent_video: bool = False,
) -> Manifest:
    """Apply `policy` across a Manifest and record a missing muxer (§14.1).

    Exists because §14.1's requirement is not per-item: "ffmpeg is absent so
    `best` means something weaker here" is a statement about the whole
    fetch, and it must be *said* -- CLI warning, `degradedReason`, GUI
    marker. Before this function no layer said it, so `best` on a machine
    without ffmpeg would have quietly handed over 720p.

    The test for "was quality withheld" is a comparison, not a guess: choose
    again with the mux variants restored and see whether the policy would
    have picked something better. A silent video is the one case that needs
    no comparison: nothing was withheld, and what was delivered is missing
    something anyway.
    """
    withheld: dict[str, list[Variant]] = {}
    left_empty: dict[str, bool] = {}
    silent: list[Variant] = []
    not_renditions = 0
    for item in manifest.items:
        renditions = _renditions(item)
        not_renditions += len(item.variants) - len(renditions)
        apply_policy(
            item, policy, mux_available=mux_available, allow_silent_video=allow_silent_video
        )
        reason = withholding_reason(item, mux_available=mux_available)
        if reason is None:
            continue
        if item.chosen is not None and item.chosen.needs_mux:
            # Only `_selectable`'s opted-in door leads here: a rendition that
            # needs muxing, chosen while the item has nothing to mux it with.
            silent.append(item.chosen)
            continue
        # Compared against the renditions, not against `variants`: a sound
        # track that got into the menu is not quality this build withheld,
        # and naming it as the loss would send the reader looking for a
        # rendition that never existed.
        unrestricted, _ = _choose_core(renditions, policy)
        if unrestricted is None or unrestricted is item.chosen:
            continue
        if item.chosen is None or _sort_key(unrestricted) > _sort_key(item.chosen):
            withheld.setdefault(reason, []).append(unrestricted)
            left_empty[reason] = left_empty.get(reason, True) and item.chosen is None

    if not_renditions:
        _record_exclusion(manifest, "audio_only", not_renditions)

    for reason, variants in withheld.items():
        best_withheld = max(variants, key=_sort_key)
        # `resolution_class`, not `height`: this sentence tells a person which
        # rung they lost, and the number a person means by "1080p" is the
        # short side (user ruling 2026-08-17). Reading `height` off a
        # 1080x1920 Reel reports a 1920p rendition, which does not exist.
        rung = resolution_class(best_withheld) or "?"
        tail = _NOTHING_LEFT_TAIL if left_empty[reason] else _FELL_BACK_TAIL
        _declare(manifest, f"{reason}: {_WITHHELD_DETAIL[reason].format(height=rung)}{tail}")

    if silent:
        rung = resolution_class(max(silent, key=_sort_key)) or "?"
        _declare(manifest, f"{SILENT_VIDEO_REASON}: {_SILENT_VIDEO_DETAIL.format(height=rung)}")
    return manifest


def to_ytdlp_format_selector(policy: Policy) -> str:
    """Bridge policy semantics to a yt-dlp `--format` selector string
    (PSM §6 table). The semantic is decided by `choose`/`_choose_core`
    above; this function only translates it for the yt-dlp adapter."""
    if policy == "best":
        return "bv*+ba/b"
    if policy == "smallest":
        return "wv*+wa/w"
    if isinstance(policy, MaxHeight):
        n = policy.value
        # Two pools, tried in order, because the selector language has no way
        # to say `min(width, height) <= n` -- there is no arithmetic in it.
        # Landscape satisfies the first (its height is the short side);
        # portrait falls through to the second, which is the same test
        # applied to the side that is short for *it*. Without the fallback
        # this bridge would keep the literal reading of `max-height` that
        # `resolution_class` exists to correct, and the two paths would
        # disagree about the same Reel -- which Phase 2 §2.3 forbids: the
        # bridge differs per platform, the semantics do not.
        return f"bv*[height<={n}]+ba/bv*[width<={n}]+ba/b[height<={n}]/b[width<={n}]/b"
    raise TypeError(f"unsupported policy: {policy!r}")
