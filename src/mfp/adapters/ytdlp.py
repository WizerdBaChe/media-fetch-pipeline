"""yt-dlp as a probe strategy (PSM §5.1 rung 1, and the seed of M7).

Lives here rather than inside `adapters/instagram/` because it is not
Instagram's: the Instagram adapter borrows it as a first attempt, and M7's
standalone YouTube/X/Bilibili adapter will use the same translation. Writing
it twice is how the two copies start disagreeing about what a `Variant` is.

`--dump-json --no-download` costs one subprocess and no browser. Against
Instagram it is expected to fail much of the time (Phase 1 H1) — the reason
to keep it first anyway is that failure is cheap and the day yt-dlp starts
working again, this path gets faster with no code change. `attempts[]` on the
Manifest is what makes that day observable rather than invisible.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable, Literal

from mfp import logs
from mfp.adapters.base import FetchContext, PlatformAdapter, standard_fetch
from mfp.captions import (
    AUTO_CAPTION_KIND,
    CAPTION_KIND,
    CAPTION_KINDS,
    choose_caption_file,
    empty_automatic_is_trustworthy,
    tracks_of,
)
from mfp.errors import (
    BudgetExhausted,
    DependencyMissingError,
    MfpError,
    NoMediaInPost,
    RateLimitedError,
    UpstreamStructureChange,
    UpstreamUnreachable,
)
from mfp.inputs import identify
from mfp.models import (
    ExcludedFormats,
    FetchResult,
    Manifest,
    ManifestSource,
    MediaItem,
    SidecarAsset,
    Variant,
)
from mfp.policy import Policy

#: Seconds before a `--dump-json` is abandoned. Generous: it is one network
#: round trip plus yt-dlp's own extractor, and killing it early would turn a
#: slow success into a false negative.
DUMP_JSON_TIMEOUT_S = 45.0

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    # argv-array invocation only -- never a shell string (Phase 2 §4.2).
    #
    # Resolution happens HERE, at the spawn, and not in `build_argv`: that
    # function is a pure argv builder with tests that read the literal
    # `yt-dlp` out of the list, and making it answer differently on a
    # machine that happens to have one installed would make those tests
    # depend on the developer's PATH.
    from mfp.mediatool import resolved_command

    argv = resolved_command(argv)
    started = time.monotonic()
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            # Never inherit our stdin -- see `doctor._default_runner` for the
            # measured failure this prevents.
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=DUMP_JSON_TIMEOUT_S,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        # A timeout is the one outcome with no return code, and the one most
        # worth having in the log: it is what a wedged probe looks like.
        logs.ran(argv[0], args=argv[1:], code=-1, ms=_elapsed_ms(started),
                 timeout=True)
        raise
    logs.ran(argv[0], args=argv[1:], code=done.returncode,
             ms=_elapsed_ms(started))
    return done


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


#: Platforms whose probe also asks for the danmaku track.
#:
#: Only Bilibili, and only because asking is not free: `--dump-json` alone
#: reports `subtitles: {}` and the extractor makes an EXTRA request to fill
#: it in. Adding the flags for every platform would spend one more poke per
#: probe everywhere to serve a feature one platform has (D-33 -- the pacing
#: guard is bot protection, so requests we do not need are not free just
#: because they are cheap).
DANMAKU_PLATFORMS: frozenset[str] = frozenset({"bilibili"})

#: yt-dlp's language key for Bilibili bullet comments. Not a real subtitle
#: track: Bilibili's actual CC subtitles need a login (measured -- yt-dlp
#: warns "Subtitles are only available when logged in"), while danmaku comes
#: back without one, which is why this works under D-2.
DANMAKU_LANG = "danmaku"


def build_argv(url: str, *, executable: str | None = None, platform: str | None = None) -> list[str]:
    argv = [executable or "yt-dlp", "--dump-json", "--no-download"]
    if platform in DANMAKU_PLATFORMS:
        # Populates `subtitles` without downloading anything: --dump-json
        # still suppresses the write, and we take the URL from the payload.
        argv += ["--write-subs", "--sub-langs", DANMAKU_LANG]
    argv.append(url)
    return argv


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Protocols this build's downloader can move: one URL, one GET, byte
#: ranges. `m3u8_native`, `dash` and friends name a playlist of fragments
#: that has to be assembled, which `download.py` does not do -- offering one
#: as a rendition would be a transfer that fails after the bytes are spent.
SINGLE_FILE_PROTOCOLS: frozenset[str] = frozenset({"https", "http"})

#: A still is a rendition too. It reaches `classify_row` with `vcodec:
#: "none"` and no audio field at all, which is the same shape an unusable
#: row has -- and dropping it silently emptied the manifest for every
#: image post yt-dlp handles.
IMAGE_EXTS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "webp", "heic"})

#: Which container each extension belongs to. The pairing matters because
#: `download.py` picks its ffmpeg muxer from the DESTINATION extension,
#: which is the chosen video's: `-f webm` with an AAC track is refused.
#: (ffmpeg itself is more permissive than this; our muxer call is not.)
_FAMILIES: dict[str, str] = {
    "mp4": "mp4",
    "m4a": "mp4",
    "m4v": "mp4",
    "mov": "mp4",
    "webm": "webm",
    "weba": "webm",
}

ROW_MEDIA = "media"
ROW_AUDIO = "audio"
ROW_STORYBOARD = "storyboard"
ROW_SEGMENTED = "segmented"
ROW_UNUSABLE = "unusable"


def _family(ext: str) -> str:
    return _FAMILIES.get(ext.lower(), "other")


def classify_row(fmt: dict[str, Any]) -> str:
    """What a yt-dlp `formats[]` row actually IS (M10).

    The list is not a quality menu. Measured against one real 4K upload: 33
    rows, of which 22 are video-only, 6 are audio-only, 4 are storyboard
    sheets, and exactly 1 is a complete progressive file. Translating all 33
    into `Variant`s -- which is what this adapter used to do -- put an
    `.m4a` and a 320x180 `mhtml` contact sheet in front of the policy
    engine as if they were renditions, and `smallest` duly returned the
    audio file.

    A row with NO codec fields at all is called media on purpose: that is
    the bare-`url` shape some extractors return in place of a format list,
    and this adapter treated it as the media long before rows had kinds.
    """
    url = fmt.get("url")
    if not isinstance(url, str) or not url:
        return ROW_UNUSABLE

    ext = str(fmt.get("ext") or "").lower()
    protocol = str(fmt.get("protocol") or "https").lower()
    note = str(fmt.get("format_note") or "").lower()
    if ext == "mhtml" or protocol == "mhtml" or "storyboard" in note:
        return ROW_STORYBOARD
    if protocol not in SINGLE_FILE_PROTOCOLS:
        return ROW_SEGMENTED

    if ext in IMAGE_EXTS:
        return ROW_MEDIA

    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")
    if vcodec is None and acodec is None:
        return ROW_MEDIA
    if vcodec not in (None, "none"):
        return ROW_MEDIA
    if acodec not in (None, "none"):
        return ROW_AUDIO
    return ROW_UNUSABLE


def _variant(fmt: dict[str, Any]) -> Variant | None:
    """One yt-dlp format row -> one Variant, or None if unusable.

    `needs_mux` is derived from yt-dlp's own codec fields rather than
    guessed: a format with `acodec: "none"` is video-only, and offering it as
    if it were complete would hand the user a silent movie.
    """
    url = fmt.get("url")
    if not isinstance(url, str) or not url:
        return None

    acodec = fmt.get("acodec")
    vcodec = fmt.get("vcodec")
    has_audio = None if acodec is None else acodec != "none"
    video_only = vcodec not in (None, "none") and acodec == "none"

    bitrate = fmt.get("tbr") or fmt.get("vbr")
    return Variant(
        url=url,
        width=_int_or_none(fmt.get("width")),
        height=_int_or_none(fmt.get("height")),
        bitrate=int(bitrate * 1000) if isinstance(bitrate, (int, float)) else None,
        ext=str(fmt.get("ext") or "mp4"),
        has_audio=has_audio,
        needs_mux=video_only,
        vcodec=vcodec if isinstance(vcodec, str) and vcodec != "none" else None,
        request_headers=_carried_headers(fmt),
        language=_language_of(fmt),
        is_original=_is_original_audio(fmt),
    )


#: What yt-dlp puts in `language_preference` for the track a video was
#: actually recorded in. Every other audio row on a multi-language upload
#: carries -1. Verified against a real 21-track payload 2026-08-30.
_ORIGINAL_LANGUAGE_PREFERENCE = 10


def _language_of(fmt: dict[str, Any]) -> str | None:
    """The language tag the source put on this row, or None if it put none.

    Not normalised. `en-US`, `es-US`, `iw` and `zh-Hant` all arrive as the
    platform spells them, and rewriting them here would invent a claim the
    source did not make -- the field exists to record what was offered, and
    `is_original` is what decides anything.
    """
    language = fmt.get("language")
    return language if isinstance(language, str) and language else None


def _is_original_audio(fmt: dict[str, Any]) -> bool | None:
    """Does the source say this row is the video's own audio (P-49)?

    `language_preference` is yt-dlp's own verdict and the only field that
    answers this: 10 for the original, -1 for every dub. It is read rather
    than the `format_note` prose ("English (US) original (default)")
    because the note is written for humans and changes with the locale.

    None when there is nothing to decide -- no preference field at all,
    which is every single-track video and every platform but YouTube. A
    row that carries a preference and is not the original answers False,
    so "we looked and it is a dub" is distinguishable from "nobody said".
    """
    preference = fmt.get("language_preference")
    if not isinstance(preference, int) or isinstance(preference, bool):
        return None
    return preference == _ORIGINAL_LANGUAGE_PREFERENCE


#: Headers we will carry from `formats[].http_headers` onto a Variant.
#:
#: An allowlist rather than a passthrough. These values arrive from a
#: subprocess and end up on our own outbound requests, so the set is the
#: smallest one that makes a measured platform work. `Cookie` is absent and
#: must stay absent: this tool carries no credentials and does not act as a
#: logged-in user (D-2). `Range` is absent because the transfer owns it --
#: letting a source dictate it would silently truncate a resume.
CARRIED_HEADERS: frozenset[str] = frozenset(
    {"Referer", "Origin", "User-Agent", "Accept", "Accept-Language"}
)


def _carried_headers(fmt: dict) -> dict[str, str] | None:
    """The allowlisted subset of what the source said this URL needs.

    Returns None rather than an empty dict when nothing survives, so a
    manifest for a platform that needs no special headers -- which is every
    platform measured before Bilibili -- serializes exactly as it did
    before this existed.
    """
    raw = fmt.get("http_headers")
    if not isinstance(raw, dict):
        return None
    carried = {
        str(key): str(value)
        for key, value in raw.items()
        if str(key) in CARRIED_HEADERS and isinstance(value, str) and value
    }
    return carried or None


#: yt-dlp phrasings that mean "this post is fine, it just has no media".
#:
#: Matched on yt-dlp's own message rather than guessed from the platform,
#: because the same URL shape can be either case. Kept deliberately narrow:
#: anything not listed here stays `upstream_structure_change`, so a real
#: parser break is never quietly downgraded to "nothing to see here" (O-10 --
#: the failure being fixed is over-broad classification, and curing it with
#: a different over-broad rule would just move the problem).
_NO_MEDIA_PHRASES: tuple[str, ...] = (
    "no video could be found in this tweet",
    "there's no video in this tweet",
)

#: The HTTP status yt-dlp forwarded, in either spelling it uses. One real
#: line carries both: `HTTP Error 412: Precondition Failed (caused by
#: <HTTPError 412: Precondition Failed>)`. `HTTP\s*Error` covers the spaced
#: and unspaced form together.
_HTTP_STATUS_RE = re.compile(r"HTTP\s*Error\s*:?\s*(\d{3})", re.I)

#: The platform said no on purpose. 412 is what Bilibili's risk control
#: answers a request it does not like (measured 2026-09-03 on a real link,
#: and the same URL succeeded four times an hour later -- so this is a
#: statement about the moment, never about the URL). 429 is the standard
#: spelling of the same thing.
_BLOCK_STATUSES: frozenset[str] = frozenset({"412", "429"})

#: The platform's own fault, forwarded. A 5xx is the server saying it is
#: broken; nothing about our parser follows from it.
_UPSTREAM_FAULT_STATUSES: frozenset[str] = frozenset({"500", "502", "503", "504"})

#: yt-dlp's wrapper for every transport-layer failure -- DNS, refused
#: connection, reset, TLS. Matched on the EXCEPTION NAME rather than on the
#: message, because the message is the operating system's and the operating
#: system translates it: this machine reports a refused connection as
#: 「無法連線，因為目標電腦拒絕連線。」, so a table of English phrases
#: ("connection refused") would classify nothing here (P-77).
_TRANSPORT_MARKER = "transporterror"

#: What a failed `yt-dlp --dump-json` was, as far as its own output can say.
#: `unknown` is the honest residual and keeps its old destination
#: (`upstream_structure_change`); every named value had to be shown a real
#: captured stderr before it was added.
YtDlpFailure = Literal["no_media", "rate_limited", "unreachable", "unknown"]


def _error_lines(stderr: str) -> list[str]:
    """The lines yt-dlp marked `ERROR:`, or all of them if it marked none.

    Severity is a filter, not decoration. A *successful* Bilibili probe ends
    with `WARNING: Subtitles are only available when logged in` -- so a
    classifier reading whole-stderr would be one phrase table away from
    calling every Bilibili failure a login wall, on the strength of a line
    that was never about the failure. The fallback exists because a failure
    with no marked line still has to be classified from something.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    marked = [line for line in lines if line.upper().startswith("ERROR:")]
    return marked or lines


def classify_failure(stderr: str) -> YtDlpFailure:
    """Name what went wrong, from yt-dlp's own error output.

    This exists because the thing it replaced was a two-way switch -- "does
    this say the post has no media, yes or no" -- whose `else` branch
    reported `upstream_structure_change`, i.e. 「頁面結構改變」, for a rate
    limit, a DNS failure, and a platform outage alike. A `default:` branch
    answering a question it cannot answer is the same defect as P-74, and
    the user-visible cost is the same: it told a reader that the program was
    broken and needed fixing, when the truth was "wait and try again".
    """
    blob = "\n".join(_error_lines(stderr)).lower()
    if any(phrase in blob for phrase in _NO_MEDIA_PHRASES):
        return "no_media"
    statuses = {match.group(1) for match in _HTTP_STATUS_RE.finditer(blob)}
    if statuses & _BLOCK_STATUSES:
        return "rate_limited"
    if statuses & _UPSTREAM_FAULT_STATUSES:
        return "unreachable"
    if _TRANSPORT_MARKER in blob:
        return "unreachable"
    return "unknown"


def _sidecars_from_dump(payload: dict) -> list[SidecarAsset]:
    """The danmaku track, when the source offered one.

    Only `danmaku` is read, not every subtitle language: Bilibili's real CC
    subtitles need a login (yt-dlp says so in a warning), and reaching for
    something that requires an account would quietly break D-2. Danmaku
    comes back without one, which is the whole reason this is possible.
    """
    subtitles = payload.get("subtitles")
    if not isinstance(subtitles, dict):
        return []
    entries = subtitles.get(DANMAKU_LANG)
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue
        return [
            SidecarAsset(
                kind=DANMAKU_LANG,
                url=url,
                ext=str(entry.get("ext") or "xml"),
                request_headers=_carried_headers(entry),
            )
        ]
    return []


def _captions_from_dump(payload: dict, want: str) -> list[SidecarAsset]:
    """The caption track the caller asked for, out of the probe payload.

    **No second yt-dlp run, and no second request** in the case that matters:
    `--dump-json` already carries `subtitles` and `automatic_captions`, and
    the URLs in them are ordinary signed links our own client can GET:
    measured 2026-09-08 against 2UpQbeAZuqA, HTTP 200 for both the `srt` and
    the `vtt` of a 157-track video, so yt-dlp's `impersonate: true` on those
    rows is a hint about how it would fetch them and not a condition of
    fetching them. That is what keeps this on the right side of D-34 -- a
    probe costs rate budget, a transfer does not, and this adds a transfer.

    Which track is `captions.choose_caption_file`'s answer, so `mfp fetch`
    and `mfp stack` both resolve `orig` the same way. What is decided here
    is only what a caption track IS on this platform.
    """
    manual, auto, language = tracks_of(payload)
    # Danmaku is not one. It rides in `subtitles` under its own key, and
    # `pick_caption_track` hands back the only written track when there is
    # exactly one -- so on a Bilibili video whose bullet comments are the
    # sole entry, asking for the spoken language would return the comments,
    # and they would be saved twice under two names. Bilibili's real CC
    # subtitles need a login and are therefore never in this dict (D-2).
    manual = (
        {key: value for key, value in manual.items() if key != DANMAKU_LANG}
        if isinstance(manual, dict)
        else {}
    )
    chosen = choose_caption_file(manual, auto, language, want)
    if chosen is None:
        return []
    return [
        SidecarAsset(
            kind=AUTO_CAPTION_KIND if chosen.automatic else CAPTION_KIND,
            url=chosen.url,
            ext=chosen.ext,
            language=chosen.language,
            request_headers=_carried_headers(chosen.source),
        )
    ]


def _short_side(variant: Variant) -> int:
    if variant.width and variant.height:
        return min(variant.width, variant.height)
    return variant.height or 0


def _pick_family(video: list[tuple[Variant, str]], audio: list[tuple[Variant, str]]) -> str | None:
    """Which container family to offer, or None when nothing needs muxing.

    A family with no audio row has no muxable rendition at all -- there is
    nothing to join its video-only streams to -- so excluding it costs no
    reachable quality. Among the families that do have audio, the one that
    reaches the highest rendition wins; mp4 breaks a tie, being the one
    every player opens.
    """
    families = {family for variant, family in video if variant.needs_mux}
    with_audio = families & {family for _, family in audio}
    if not with_audio:
        return None

    def ceiling(name: str) -> tuple[int, int]:
        best = max(
            (_short_side(v) for v, f in video if f == name and v.needs_mux),
            default=0,
        )
        return best, 1 if name == "mp4" else 0

    return max(with_audio, key=ceiling)


def pick_audio_track(
    candidates: list[Variant], *, wanted_language: str | None = None
) -> Variant | None:
    """Which audio track to pair with the video (P-49).

    **Bitrate cannot decide this and never could.** YouTube publishes every
    dub of a video at the same bitrate as its original -- measured on one
    talk: 21 tracks, all 129.473 kbps -- so `max(by bitrate)` was a 21-way
    tie broken by list order, and the list is enumerated `[ar]` first. The
    archive got an Arabic AI dub of an English talk, with English subtitles
    beside it and nothing anywhere recording that it had happened.

    So language leads and bitrate only breaks ties within it:

    1. the language the caller ASKED for, if any row speaks it;
    2. the row the source marks as the original;
    3. bitrate, exactly as before.

    Step 3 alone is what every platform without dubs takes, and its
    behaviour there is unchanged: one candidate, or several that differ in
    bitrate, resolve identically to the old rule.

    Returns None only when there is nothing to choose from.
    """
    if not candidates:
        return None

    if wanted_language:
        wanted = wanted_language.casefold()
        spoken = [
            v
            for v in candidates
            if v.language and language_matches(v.language.casefold(), wanted)
        ]
        if spoken:
            candidates = spoken

    return max(candidates, key=lambda v: (v.is_original is True, v.bitrate or 0))


def language_matches(offered: str, wanted: str) -> bool:
    """`en` asked for, `en-US` offered -- that is a match.

    A request is honoured at the granularity it was made: `en` takes any
    English, `en-US` takes only that one. The reverse is deliberately not
    true, so asking for a specific regional variant cannot silently hand
    back a different region's dub.
    """
    if offered == wanted:
        return True
    return offered.startswith(f"{wanted}-") or offered.startswith(f"{wanted}_")


def manifest_from_dump(
    payload: dict[str, Any],
    *,
    platform: str,
    url: str,
    audio_language: str | None = None,
    caption_language: str | None = None,
) -> Manifest:
    """Translate one `--dump-json` object into a Manifest.

    yt-dlp describes a single entry with many `formats`; that maps to one
    `MediaItem` with many `variants`, not to many items. A playlist (`entries`)
    is deliberately not handled: this project fetches individual posts, and
    quietly expanding a playlist would be the account-crawl that D-2 forbids.

    `audio_language` is the user asking for a specific dub. Absent -- which
    is every call that does not go out of its way -- the video's own audio
    wins; see `pick_audio_track`.

    `caption_language` is `mfp fetch --write-subs`, and None -- every call
    that did not ask -- records no caption track at all. `orig` means the
    language the video was SPOKEN in, never a translation of it: this is
    the same ruling as `audio_language`'s default one field along, and the
    same reason (D-146/P-49).
    """
    formats = payload.get("formats")
    if not isinstance(formats, list) or not formats:
        # Some extractors return a bare `url` with no format list.
        formats = [payload] if payload.get("url") else []

    video: list[tuple[Variant, str]] = []
    audio: list[tuple[Variant, str]] = []
    excluded: dict[str, int] = {}
    for row in formats:
        if not isinstance(row, dict):
            continue
        kind_of_row = classify_row(row)
        if kind_of_row == ROW_UNUSABLE:
            continue
        variant = _variant(row)
        if variant is None:
            continue
        if kind_of_row == ROW_MEDIA:
            video.append((variant, _family(variant.ext)))
        elif kind_of_row == ROW_AUDIO:
            audio.append((variant, _family(variant.ext)))
        else:
            excluded[kind_of_row] = excluded.get(kind_of_row, 0) + 1

    # Every audio row leaves the candidate list, including the one kept as
    # the paired track: an audio stream is not a quality option for a video.
    if audio:
        excluded["audio_only"] = len(audio)

    family = _pick_family(video, audio)
    if family is None:
        variants = [variant for variant, _ in video]
        track = None
    else:
        # A progressive row needs no muxing, so its container is its own
        # business and it stays whatever family it is.
        variants = [v for v, f in video if not v.needs_mux or f == family]
        mismatched = len(video) - len(variants)
        if mismatched:
            excluded["container_mismatch"] = mismatched
        track = pick_audio_track(
            [v for v, f in audio if f == family], wanted_language=audio_language
        )

    kind = "video" if payload.get("vcodec") != "none" and not _looks_like_image(payload) else "image"

    sidecars = _sidecars_from_dump(payload)
    captions_unconfirmed = False
    if caption_language:
        chosen_captions = _captions_from_dump(payload, caption_language)
        sidecars += chosen_captions
        if not chosen_captions:
            # Q2: "no track was chosen" has two causes the payload alone
            # cannot tell apart -- nobody made one, or the automatic list
            # came back empty from an extractor that answers a refusal the
            # same way (P-84). Computed from the SHAPE of this payload, never
            # from its size: `empty_automatic_is_trustworthy` is the same
            # test `captions.py` uses for the read-time version of this call.
            _, auto, _ = tracks_of(payload)
            if not auto and not empty_automatic_is_trustworthy(payload):
                captions_unconfirmed = True

    items = (
        [
            MediaItem(
                index=0,
                kind=kind,
                variants=variants,
                audio=track,
                sidecars=sidecars,
            )
        ]
        if variants
        else []
    )
    return Manifest(
        source=ManifestSource(
            platform=platform,
            url=url,
            id=str(payload.get("id") or ""),
            author=payload.get("uploader") or payload.get("channel"),
            caption=payload.get("description") or payload.get("title"),
            timestamp=payload.get("upload_date"),
        ),
        items=items,
        excluded=[
            ExcludedFormats(reason=reason, count=count)
            for reason, count in sorted(excluded.items())
            if count
        ],
        captions_unconfirmed=captions_unconfirmed,
    )


def _looks_like_image(payload: dict[str, Any]) -> bool:
    return payload.get("ext") in IMAGE_EXTS


class YtDlpUnavailable(Exception):
    """yt-dlp could not be run, or produced nothing usable.

    Not an `MfpError`: at rung 1 of a strategy chain this is a normal,
    expected outcome that the caller records and moves past. Raising a
    taxonomy error would invite a caller to surface it to the user as though
    the whole probe had failed.

    `stderr` is the WHOLE of what yt-dlp printed, and it is carried rather
    than summarised because the summary is one line and classification needs
    the rest: yt-dlp puts the cause in `(caused by ...)` on the same line
    sometimes and on another line other times. Empty when the failure was
    ours (unparseable stdout, no usable format) rather than yt-dlp's, which
    is exactly the case that must stay `upstream_structure_change`.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class YtDlpMissing(YtDlpUnavailable):
    """The binary itself could not be launched.

    A subclass so `except YtDlpUnavailable` -- which is what the Instagram
    chain does, and correctly -- still catches it. The distinction matters
    only where yt-dlp is the *only* strategy: "you have not installed it"
    (exit 6, actionable in one command) and "it ran and found nothing"
    (exit 5, nothing the user can do) are different answers, and collapsing
    them sends the reader to reinstall a working binary.
    """


def probe(
    url: str,
    *,
    platform: str,
    executable: str | None = None,
    runner: Runner | None = None,
    audio_language: str | None = None,
    caption_language: str | None = None,
) -> Manifest:
    """Run `yt-dlp --dump-json` and translate the result.

    Raises `YtDlpUnavailable` for every failure mode — missing binary,
    non-zero exit, unparseable stdout, or a payload with no usable format.
    The caller records the attempt and tries the next strategy.
    """
    argv = build_argv(url, executable=executable, platform=platform)
    try:
        completed = (runner or default_runner)(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise YtDlpMissing(f"could not run {argv[0]}: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr or ""
        # The last ERROR line, not the last line. yt-dlp is free to print a
        # warning after the error that killed it, and quoting that warning
        # as the reason is how a reader ends up debugging the wrong thing.
        marked = _error_lines(stderr)
        raise YtDlpUnavailable(
            f"yt-dlp exited {completed.returncode}: {marked[-1] if marked else 'no output'}",
            stderr=stderr,
        )

    try:
        payload = json.loads((completed.stdout or "").strip().splitlines()[0])
    except (json.JSONDecodeError, IndexError) as exc:
        raise YtDlpUnavailable("yt-dlp produced no parseable JSON") from exc

    if not isinstance(payload, dict):
        raise YtDlpUnavailable(f"yt-dlp returned a {type(payload).__name__}, expected an object")

    manifest = manifest_from_dump(
        payload,
        platform=platform,
        url=url,
        audio_language=audio_language,
        caption_language=caption_language,
    )
    if not manifest.items:
        raise YtDlpUnavailable("yt-dlp returned no downloadable formats")
    return manifest


#: Everything `inputs.identify` can name that this adapter, not the browser
#: chain, is responsible for. `generic` is in the list by O-10: an
#: unrecognised host is delegated to yt-dlp rather than refused, which is
#: what makes the thousand-plus sites it already handles reachable.
PLATFORMS: frozenset[str] = frozenset({"youtube", "x", "bilibili", "generic"})


class YtDlpAdapter(PlatformAdapter):
    """The non-Meta platforms (PSM §12 M7), wired into the registry by M8.

    `probe` is the translation above. `fetch` is the same transfer path
    every other adapter uses -- yt-dlp hands out direct media URLs and
    `download.py` moves bytes, so there is no reason for a second
    downloader here.

    **What this does not do**: yt-dlp can itself download and mux a DASH
    video+audio pair, and this build does not ask it to. `manifest_from_dump`
    marks video-only formats `needsMux` but records no second stream, so the
    policy layer withholds them and reports `no_audio_track_progressive_only`
    (§14.1). The consequence is real and worth stating: on YouTube, `best`
    means the best *combined* format, not the best format. Delegating the
    transfer to `yt-dlp -f <selector> -o <template>` is the named upgrade,
    and `to_ytdlp_format_selector` already exists for it -- but a declared
    ceiling beats a silent one, and that is the whole of §14.1.
    """

    name = "ytdlp"

    def __init__(self, *, probe_fn: Callable[..., Manifest] | None = None) -> None:
        self._probe = probe_fn or probe

    @staticmethod
    def _platform_of(url: str) -> str | None:
        from urllib.parse import parse_qsl, urlsplit

        split = urlsplit(url)
        identified = identify(
            split.hostname or "", split.path, dict(parse_qsl(split.query))
        )
        return identified[0] if identified else None

    def matches(self, url: str) -> bool:
        return self._platform_of(url) in PLATFORMS

    def probe(self, url: str, ctx: FetchContext) -> Manifest:
        """One subprocess, one budget slot.

        The slot is taken even though yt-dlp holds the socket rather than
        this process: the governor counts pokes at the platform, and which
        process made them does not change how the platform experienced it.
        """
        platform = self._platform_of(url) or "generic"
        ctx.budget.acquire(platform)

        try:
            manifest = self._probe(
                url,
                platform=platform,
                executable=ctx.config.binaries.yt_dlp,
                audio_language=ctx.audio_language,
                caption_language=ctx.caption_language,
            )
        except YtDlpMissing as exc:
            raise DependencyMissingError(
                f"yt-dlp is required for {platform} URLs and could not be run: {exc}"
            ) from exc
        except YtDlpUnavailable as exc:
            # `str(exc)` is the fallback for the three raise sites that have
            # no stderr to carry -- those are our own parse failures, and
            # they classify as `unknown`, which is where they belong.
            kind = classify_failure(exc.stderr or str(exc))
            if kind == "no_media":
                raise NoMediaInPost(
                    f"{url} was read successfully and contains nothing this tool can "
                    f"download: {exc}",
                    url=url,
                ) from exc
            if kind == "rate_limited":
                # The governor hears about this at the pipeline's one choke
                # point, not here: every adapter's block signal has to enter
                # the cooldown the same way, and only one of them used to.
                raise RateLimitedError(
                    f"{platform} refused this request rather than failing to answer "
                    f"it -- the same URL usually works later: {exc}",
                    url=url,
                ) from exc
            if kind == "unreachable":
                raise UpstreamUnreachable(
                    f"{platform} could not be reached or answered with its own error "
                    f"for {url}: {exc}",
                    url=url,
                ) from exc
            raise UpstreamStructureChange(
                f"yt-dlp ran but returned nothing usable for {url}: {exc}", url=url
            ) from exc

        if manifest.captions_unconfirmed:
            manifest = self._retry_unconfirmed_captions(manifest, url, platform, ctx)
        return manifest

    def _retry_unconfirmed_captions(
        self, first: Manifest, url: str, platform: str, ctx: FetchContext
    ) -> Manifest:
        """Q2: ask exactly once more when captions came back unconfirmed.

        "None" and "could not ask" are indistinguishable in a single payload
        from an extractor in `SILENT_ON_REFUSAL_EXTRACTORS` (P-84), so one
        re-read is the whole of what this can do about it -- never a loop,
        because the measured shape is empty reads arriving in RUNS, and more
        of the same read is more of what may be being refused.

        Paid for as a read-only lookup (`lookup_bucket`), same as every other
        second look this product takes at a page it already probed. And this
        may never fail the PROBE: a caller who already has `first` has a
        complete manifest, just not a confirmed answer about captions, so
        every failure mode here is swallowed and logged rather than raised.
        """
        from mfp.config import lookup_bucket

        try:
            ctx.budget.acquire(lookup_bucket(platform))
            second = self._probe(
                url,
                platform=platform,
                executable=ctx.config.binaries.yt_dlp,
                audio_language=ctx.audio_language,
                caption_language=ctx.caption_language,
            )
        # Every CLASSIFIED outcome (budget, rate limit, unreachable, structure
        # change, missing yt-dlp ...) is caught: the first read already
        # succeeded, and a network blip on the second must not undo it. An
        # unclassified exception is a bug and still surfaces.
        except (MfpError, YtDlpUnavailable) as exc:
            logs.record(
                "ytdlp.caption_retry_failed",
                url=logs.safe_url(url),
                reason=type(exc).__name__,
            )
            return first

        if any(
            sidecar.kind in CAPTION_KINDS
            for item in second.items
            for sidecar in item.sidecars
        ):
            return second
        logs.record("ytdlp.caption_retry_still_unconfirmed", url=logs.safe_url(url))
        return first

    def fetch(self, manifest: Manifest, policy: Policy, ctx: FetchContext) -> FetchResult:
        return standard_fetch(manifest, policy, ctx, fallback_platform=self.name)


__all__ = [
    "DUMP_JSON_TIMEOUT_S",
    "PLATFORMS",
    "YtDlpAdapter",
    "YtDlpFailure",
    "YtDlpMissing",
    "YtDlpUnavailable",
    "build_argv",
    "classify_failure",
    "default_runner",
    "manifest_from_dump",
    "pick_audio_track",
    "language_matches",
    "probe",
]
