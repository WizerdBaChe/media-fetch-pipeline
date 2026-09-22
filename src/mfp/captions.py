"""Which caption track a video has, and getting one onto disk.

Split out of `stack.py` (2026-08-30): `transcript` and the frame stacker
both need to find and fetch a caption track, and neither of them is the
other's reason for existing. What lives here is the yt-dlp side of
captions -- discovery, choosing between tracks, downloading one. Parsing
what comes back is `cues.py`, which runs no programs at all.

The ruling this module carries is `ORIGINAL_LANG`: asking YouTube for `en`
on a Mandarin video returns fluent English that nobody said, so the default
is the track the video was SPOKEN in and a translation has to be asked for
by name. It is the same failure shape as P-49 one layer up -- a plausible
artifact of the right language and the wrong provenance.

**This is CORE, and was an extension until 2026-09-08** (`INV-P9`, ruling
R9). The old classification was right while the only callers were
`transcript` and `stack`. It stopped being right when `mfp fetch
--write-subs` landed: taking the platform's own caption track is
ACQUISITION under D-146's criterion as restated by D-149 -- no judgement
model is asked anything, the bytes that come back are checkable against
what was requested, and the track comes out of the SAME `--dump-json` the
media probe already paid for, so it costs no request of its own (D-34).
`test_layering.py` defines core as "the list a standalone tool would depend
on if it were extracted tomorrow"; the transcript family is being extracted
as `local-transcript-maker`, and the user's 2026-09-08 ruling puts caption
discovery and download on mfp's side of that seam.

What deliberately did NOT follow it: `cues.py`. Parsing a caption file and
deciding what a cue is is analysis, it runs no programs, and it goes with
the transcript family. Nothing here imports it, and now that this module is
core the layering gate FAILS if anything ever does, rather than a reviewer
having to notice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mfp import logs
from mfp.errors import StackError, UsageError
from mfp.mediatool import run_tool

__all__ = [
    "AUTO_CAPTION_KIND",
    "CAPTION_KIND",
    "CAPTION_KINDS",
    "UNTRUSTWORTHY_EMPTY_CAPTIONS_EXTRACTORS",
    "ORIGINAL_LANG",
    "SAVEABLE_FORMATS",
    "CaptionFile",
    "caption_sidecars",
    "caption_tracks",
    "choose_caption_file",
    "empty_automatic_is_trustworthy",
    "fetch_captions",
    "pick_caption_track",
    "resolve_caption_source",
    "tracks_of",
    "video_metadata",
]


#: What `--sub-lang` means when nobody says otherwise: the track the video
#: was actually spoken in, not a machine translation of it. Asking YouTube
#: for `en` on a Mandarin video returns fluent English that nobody said.
ORIGINAL_LANG = "orig"

#: `SidecarAsset.kind` for a caption track the platform's own people wrote,
#: and for one a machine produced. Two kinds rather than one kind plus a
#: flag, because they are two different things to a reader: a written track
#: carries punctuation and speaker turns, an ASR track carries neither and
#: is wrong about names. The distinction is in the wire value on purpose --
#: it is what an archive can still answer a year later.
CAPTION_KIND = "captions"
AUTO_CAPTION_KIND = "auto-captions"
CAPTION_KINDS: frozenset[str] = frozenset({CAPTION_KIND, AUTO_CAPTION_KIND})

#: Caption formats we save exactly as the platform serves them, best first.
#:
#: `srt` leads because it is the format the rest of this product writes
#: (`fetch_captions` asks yt-dlp to convert to it) and because YouTube's
#: `vtt` for an automatic track is the karaoke form: measured 2026-09-08 on
#: 2UpQbeAZuqA, the same track is 77,939 characters as srt and 357,495 as
#: vtt, the difference being a `<c>` span per word.
#:
#: A track offered in NEITHER is not saved at all. `json3` and `srv3` are
#: real caption formats and no player, no `cues.caption_cues` and no human
#: opens one -- writing the blob out would put a file in the folder that
#: looks like captions and is not.
SAVEABLE_FORMATS: tuple[str, ...] = ("srt", "vtt")

#: The formats a real caption track is published in. Wider than
#: `SAVEABLE_FORMATS` on purpose, because the two paths have different powers:
#: `choose_caption_file` takes the platform's URL as it stands and so needs srt
#: or vtt, while `fetch_captions` runs yt-dlp with `--convert-subs srt` and can
#: take any of these.
#:
#: What it EXCLUDES is the point. YouTube publishes the live-chat replay inside
#: `subtitles`, beside the real tracks, under the key `live_chat` and in bare
#: `json` -- which is not `json3`, the caption format. It is a chat log, not a
#: transcript of anything said. Measured 2026-09-09 on two videos:
#: `cAeszOrPGRo` offers `live_chat` as its ONLY written track, and
#: `QrNAMEg4xDw` offers `live_chat` beside a written `zh-TW`.
CAPTION_FORMATS: frozenset[str] = frozenset(
    {"srt", "vtt", "ttml", "srv1", "srv2", "srv3", "json3"}
)


def _is_caption_track(entries: object) -> bool:
    """Whether one track entry list is a caption track at all.

    Structural, never by key name (D-155): a track counts when it is published
    in a format captions are published in. Naming `live_chat` here would catch
    today's instance and nothing else, and a key is the extractor's word.

    Shape-checked for the same reason `choose_caption_file` is (P-72): this
    comes from a subprocess, and an entry is free to be a string or a null.
    """
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and str(entry.get("ext") or "").lower() in CAPTION_FORMATS:
            return True
    return False


def _caption_tracks_only(tracks: object) -> dict:
    """The tracks that are captions, keyed as the platform keyed them."""
    if not isinstance(tracks, dict):
        return {}
    return {key: value for key, value in tracks.items() if _is_caption_track(value)}


@dataclass(frozen=True)
class CaptionFile:
    """One caption track, resolved to a single file that could be saved.

    `source` is the row the platform published, carried whole so a caller
    that has to build request headers can apply its own allowlist to it --
    this module does not decide which headers leave the machine (D-2).
    """

    language: str
    automatic: bool
    url: str
    ext: str
    source: dict


def choose_caption_file(
    manual: dict, auto: dict, language: str | None, want: str = ORIGINAL_LANG
) -> CaptionFile | None:
    """`pick_caption_track` narrowed to one downloadable file, or None.

    None rather than an exception, which is the whole difference from
    `pick_caption_track`: this one is called on the download path, and a
    video that has no captions is not a failed download. The caller says so
    once, on stderr, and saves the media.

    The track lists are shape-checked rather than trusted (P-72). They come
    from a subprocess, one entry per format, and an extractor is free to
    hand back a string, a null, or an entry with no url at all.
    """
    if not isinstance(manual, dict):
        manual = {}
    if not isinstance(auto, dict):
        auto = {}

    try:
        key, is_auto = pick_caption_track(manual, auto, language, want)
    except StackError:
        return None

    entries = (auto if is_auto else manual).get(key)
    by_ext: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        ext = str(entry.get("ext") or "").lower()
        if isinstance(url, str) and url and ext and ext not in by_ext:
            by_ext[ext] = entry

    for ext in SAVEABLE_FORMATS:
        entry = by_ext.get(ext)
        if entry is not None:
            return CaptionFile(
                language=key,
                automatic=is_auto,
                url=str(entry["url"]),
                ext=ext,
                source=entry,
            )
    return None


def video_metadata(
    url: str, *, executable: str | None = None, budget: object | None = None
) -> dict:
    """One yt-dlp metadata read, whole, and it takes a budget slot.

    Split out of `caption_tracks` so a caller like `mfp stack` can take the
    id and the title out of the SAME read rather than paying for a second
    one -- this is a platform request, and the budget governor exists
    because they are not free.

    **It said that for a year and did not do it.** This is a probe by D-34's
    own definition -- it pokes a page endpoint, not a CDN -- and until
    2026-09-09 nothing on this path acquired anything. `mfp fetch` was fine,
    because its probe goes through the adapter, which does; `mfp stack`
    reads metadata itself and so poked YouTube at whatever rate a person or
    a script pressed enter. Four back-to-back listing runs were four
    unpaced requests, which is the shape D-33 exists to prevent, and this
    build could do it all day.

    Paying for the read is right on D-34's terms alone. It is NOT claimed as
    the fix for `UNTRUSTWORTHY_EMPTY_CAPTIONS_EXTRACTORS` -- a rested first read still
    came back empty, so whatever that is, unpaced probing is not all of it.

    The governor is per-process but its window is a FILE, so a CLI verb
    building its own still shares the hour with the server's (`budget.py`).
    """
    _acquire_slot(url, budget)
    raw = run_tool([
        executable or "yt-dlp", "-J", "--no-download", "--no-warnings", url,
    ])
    return json.loads(raw or "{}")


def _acquire_slot(url: str, budget: object | None) -> None:
    """Pace one metadata read, and never fail the verb for lack of pacing.

    Imported here rather than at module scope to keep `mfp doctor`'s import
    cost down, the same reason `toolchain` is imported lazily elsewhere.

    A `BudgetExhausted` is the governor doing its job and is allowed out. Any
    OTHER failure -- an unreadable state file, a platform this build cannot
    name -- must not turn 「read a video's captions」 into a crash: the read
    is what the user asked for and the pacing is a courtesy to the platform.
    """
    from mfp.errors import BudgetExhausted

    from mfp.config import lookup_bucket

    try:
        governor = budget if budget is not None else _default_governor()
        governor.acquire(lookup_bucket(_platform_of(url)))  # type: ignore[attr-defined]
    except BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001 - see the docstring
        logs.record("captions.unpaced_read", url=logs.safe_url(url))


def _platform_of(url: str) -> str:
    """The platform this URL belongs to, or `default`.

    `inputs.identify` and nothing hand-rolled: two spellings of 「youtube」
    would be two sliding hours, which is not a smaller budget but a bigger
    one. The bucket this feeds is `lookup:<platform>` (user ruling
    2026-09-09) -- a question that downloads nothing gets its own window,
    still per-platform, on looser limits than a transfer.

    `default` for anything it does not recognise, which is the right way to
    fail: `BudgetConfig.for_platform` already routes unknown platforms there,
    and a read that cannot be attributed is still a read.
    """
    from urllib.parse import parse_qsl, urlsplit

    from mfp.inputs import identify

    parts = urlsplit(url)
    identified = identify(
        parts.netloc.lower(), parts.path, dict(parse_qsl(parts.query))
    )
    return identified[0] if identified else "default"


def _default_governor():
    """The process's governor -- the SAME object the fetch path uses.

    This function cached one of its own for exactly one afternoon, and the
    comment above it said 「one governor per process」 while the code made
    one per MODULE. Inside the GUI's server that is two live governors: the
    worker's and this one. Each reads the state file at construction and
    writes it back whole, so they do not share an hour, they overwrite each
    other's account of it -- a pacing guard that quietly under-counts, which
    is the one failure mode D-33 says this component exists to prevent
    (P-85). `budget.shared_governor` is the fix and the class's own
    docstring is where the requirement was written all along.
    """
    from mfp.budget import shared_governor
    from mfp.config import load_config

    return shared_governor(load_config())


#: Extractors whose empty automatic-caption list cannot be believed.
#:
#: Measured 2026-09-09 on `cAeszOrPGRo`, a video with 158 automatic tracks.
#: `-J` answered `automatic_captions: {}` on 2 of 6 reads, then 3 of 6 again
#: after a seven-minute rest -- INCLUDING the first read of the rested batch.
#: Exit 0, no stderr, all 74 top-level keys present, every other field
#: identical in shape; only the payload SIZE differed (157 KB against
#: 697 KB), and a program may not classify on size. Pinning a player client
#: does not help: `player_client=android` rolled 158/158/0/158, and every
#: other client this build could reach exited 1.
#:
#: **The cause is not established, and this constant does not claim one.**
#: (2026-09-23: the rate-limit lead below is refuted for the one episode
#: measured with raw pages -- see the Z22 paragraph further down.)
#: The lead at the time was a rate limit: asking the same platform for the FILE
#: (`--write-auto-subs`) returned an explicit `HTTP Error 429` on 4 of 8
#: attempts in the same session, and yt-dlp surfaces the 429 there while
#: reporting nothing on `-J`. What is NOT shown is that any particular empty
#: `-J` carries that 429 -- the two are correlated in time and were never
#: observed in the same read. Naming 429 in a user-facing message would be
#: D-155's own error one layer up, so nothing here does.
#:
#: What follows from the measurement alone is enough: on this extractor an
#: empty list and a genuine absence are indistinguishable, so the product
#: must not report one as the other (P-72).
#:
#: **The two lists DO fail together; the asymmetry once claimed here was the
#: instrument's.** The "1 written track" that survived every empty read was
#: `live_chat` -- the replay of this premiere's chat, which yt-dlp adds
#: whatever the player response says and `_caption_tracks_only` drops. Both
#: real lists come out of ONE renderer (`captions.playerCaptionsTracklist
#: Renderer`), so when it is absent, written and automatic vanish at once.
#: An empty written list is therefore no more trustworthy than an empty
#: automatic one on this extractor.
#:
#: **Where the empty list comes from (measured 2026-09-23, Phase Z22, P-95).**
#: Kept raw pages with `--write-pages -v` on yt-dlp 2026.08.19: every player
#: response YouTube sent for `cAeszOrPGRo` -- visionos, web, tv, web_safari,
#: mweb (android_vr UNPLAYABLE) -- was `playabilityStatus: OK` with
#: `streamingData` and NO `captions` key, and so was the page a real
#: logged-out Chrome loaded. In the same minute, from the same address, the
#: control `2UpQbeAZuqA` carried its ASR track and 156 translation languages.
#: So the empty list is YouTube's own answer about THIS video, not a yt-dlp
#: parse, not a player-client choice, not a missing PO Token (yt-dlp's
#: subtitle-skip debug line was absent), and not an address-wide refusal.
#: WHY YouTube withholds the renderer is not observable from outside; the
#: same video carried 158 tracks on 2026-09-09, so the answer can change and
#: an empty read still cannot be reported as 「none」.
#:
#: **Not reproducible on demand.** 2026-09-09, later the same day: 24 reads
#: of the same video returned `written=1, auto=158` every single time --
#: 8 through the shipped paced path, 8 with a governor that grants
#: instantly (back to back, the harsher arm), 8 more paced. Both arms in the
#: same minutes, so this is pacing against no pacing with the time of day
#: held still, and it rules pacing OUT as the explanation for the earlier
#: run. Whatever the condition is, it is external and episodic, and a test
#: that waits for it would hang.
#:
#: Retrying was tried and removed. Three reads per answer still answered
#: wrongly 2 times in 10 and cost 18 reads for 10 answers, because the empty
#: reads arrive in RUNS rather than independently -- and if the lead is
#: right, a retry is more of the thing being refused. The user ruled on
#: 2026-09-09 that there is no automatic retry and no 「wait and try again」
#: in the message either: a reader must not be sent to spend their time on
#: an answer nobody can promise.
#:
#: review-when: yt-dlp starts surfacing a cause on `-J` (a non-zero exit, a
#: warning, a distinguishable field), at which point the read can be
#: classified structurally and this set stops being needed.
UNTRUSTWORTHY_EMPTY_CAPTIONS_EXTRACTORS: frozenset[str] = frozenset({"youtube"})


def empty_automatic_is_trustworthy(data: dict) -> bool:
    """Whether an empty automatic list may be reported as 「this video has none」.

    P-72, at the one place the answer is formed: a missing list is an ERROR,
    not an empty one, and 「沒有」 and 「問不到」 are different things to be
    told. On an extractor in `UNTRUSTWORTHY_EMPTY_CAPTIONS_EXTRACTORS` the two are
    indistinguishable in the payload, so the honest answer is the second one.
    """
    return str(data.get("extractor") or "").lower() not in UNTRUSTWORTHY_EMPTY_CAPTIONS_EXTRACTORS


def caption_tracks(url: str, *, executable: str | None = None
                   ) -> tuple[dict, dict, str | None]:
    """`(manual, automatic, spoken_language)` from one yt-dlp metadata read."""
    return tracks_of(video_metadata(url, executable=executable))


def tracks_of(data: dict) -> tuple[dict, dict, str | None]:
    """The caption half of a metadata read, for a caller that already has one.

    Non-caption tracks are dropped HERE, at the one place a payload becomes
    tracks, rather than in each chooser downstream. `choose_caption_file` knew
    to reject them and `pick_caption_track` did not, so the filter ran after
    the decision that needed it: a video whose only written track was
    `live_chat` was announced as having written captions and then failed to
    download them, and a video with a real written `zh-TW` beside `live_chat`
    was refused with 「could not tell which caption track is the original
    language」 -- a cause the program had not determined (D-155), since there
    was exactly one caption track. Both measured 2026-09-09; see P-80.
    """
    return (
        _caption_tracks_only(data.get("subtitles")),
        _caption_tracks_only(data.get("automatic_captions")),
        data.get("language"),
    )


def pick_caption_track(manual: dict, auto: dict, language: str | None,
                       want: str) -> tuple[str, bool]:
    """Choose a caption track. Returns `(language_key, is_automatic)`.

    Human-written captions beat machine ones at equal language, because they
    carry punctuation and speaker turns that a stacked quote image shows off.
    """
    if want != ORIGINAL_LANG:
        if want in manual:
            return want, False
        if want in auto:
            return want, True
        raise StackError(
            f"no {want!r} captions on that video; "
            f"available: {_describe_tracks(manual, auto)}"
        )

    if language and language in manual:
        return language, False
    if len(manual) == 1:
        return next(iter(manual)), False
    for key in ((f"{language}-orig",) if language else ()):
        if key in auto:
            return key, True
    # The video's OWN declared language outranks a `-orig` suffix on somebody
    # else's track. These were the other way round, and the generic scan below
    # is unconditional, so it answered for every video that had any `-orig`
    # track at all. Measured 2026-09-09 on `cAeszOrPGRo`: spoken language
    # `zh-Hant`, an automatic `zh-Hant` track present, and `en-US-orig` also
    # present -- the scan returned English for a Mandarin video, which is the
    # one thing `--sub-lang`'s help promises the default will never do
    # (D-156/P-49: a translation has to be NAMED). The scan keeps its job for
    # the case it was written for, a video whose language is not reported.
    if language and language in auto:
        return language, True
    orig = [k for k in auto if k.endswith("-orig")]
    if orig:
        return orig[0], True
    raise StackError(
        "could not tell which caption track is the original language; "
        f"pass --sub-lang. Available: {_describe_tracks(manual, auto)}"
    )


def _describe_tracks(manual: dict, auto: dict) -> str:
    """Automatic captions run to 100+ machine translations, so listing them
    all would bury the few that matter."""
    parts = []
    if manual:
        parts.append("written " + ", ".join(sorted(manual)[:12]))
    if auto:
        keys = sorted(auto)
        head = [k for k in keys if k.endswith("-orig")] + keys[:8]
        parts.append(
            "automatic " + ", ".join(dict.fromkeys(head))
            + (f" (+{len(keys) - len(set(head))} more)" if len(keys) > 8 else "")
        )
    return "; ".join(parts) or "none"


def caption_sidecars(video: Path) -> list[Path]:
    """Caption files already sitting beside a video, newest name first.

    `stack` is run more than once on the same download -- to move the window,
    to change the strip cap -- and each run used to fetch the captions again.
    Saving them under the video's own stem makes the second run a local read,
    and makes the file discoverable by a person who is wondering what text
    the image was built from.

    Matched by prefix rather than by `Path.glob`, because a downloaded video
    is named after a post and a post title is free to contain `[`.
    """
    if not video.parent.is_dir():
        return []
    return sorted(
        p for p in video.parent.iterdir()
        if p.is_file() and p.suffix.lower() in (".srt", ".vtt")
        and p.name.startswith(video.stem + ".")
    )


def fetch_captions(url: str, key: str, is_auto: bool, dest_dir: Path, *,
                   stem: str = "captions", executable: str | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    run_tool([
        executable or "yt-dlp", "--skip-download", "--no-warnings",
        "--write-auto-subs" if is_auto else "--write-subs",
        "--sub-langs", key, "--convert-subs", "srt",
        "-o", str(dest_dir / f"{stem}.%(ext)s"), url,
    ])
    # yt-dlp puts the language between the stem and the extension, so the
    # name it chose is not knowable in advance -- only its prefix is.
    found = sorted(
        p for p in dest_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".srt"
        and p.name.startswith(stem + ".")
    )
    if not found:
        raise StackError(
            f"yt-dlp reported success but wrote no {key!r} caption file"
        )
    return found[0]


def resolve_caption_source(
    video: Path,
    *,
    subs: str | None = None,
    source_url: str | None = None,
    sub_lang: str = ORIGINAL_LANG,
    yt_dlp: str | None = None,
    say=None,
) -> Path | None:
    """Where this run's captions come from, in the order a person would try.

    A named file; a post URL named on `--subs`; the URL the video itself came
    from. Whichever it is, a caption file already sitting beside the video
    wins over fetching it again -- `stack` gets run repeatedly on the same
    download while a window or a strip count is tuned, and each of those runs
    used to be a platform request.

    Lives here rather than in the CLI because the job surface needs the same
    order, and two implementations of "where do the captions come from" is
    exactly the drift the GUI is supposed not to introduce.

    Returns None when there is nothing to resolve -- the caller is on the
    burned-in path, or has neither source and must say so itself.
    """
    say = say or (lambda _m: None)
    named_url = subs if subs and "://" in subs else None
    if subs and not named_url:
        path = Path(subs).expanduser()
        if not path.is_file():
            raise UsageError(f"no such caption file: {path}")
        return path

    fetch_from = named_url or source_url
    if not fetch_from:
        return None

    beside = caption_sidecars(video)
    if beside:
        say(f"using captions already beside the video: {beside[0].name}")
        return beside[0]

    manual, auto, language = caption_tracks(fetch_from, executable=yt_dlp)
    key, is_auto = pick_caption_track(manual, auto, language, sub_lang)
    say(f"using {'automatic' if is_auto else 'written'} {key} captions")
    found = fetch_captions(
        fetch_from, key, is_auto, video.parent, stem=video.stem, executable=yt_dlp
    )
    say(f"captions saved beside the video: {found.name}")
    return found
