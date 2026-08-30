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
"""

from __future__ import annotations

import json
from pathlib import Path

from mfp.errors import StackError
from mfp.mediatool import run_tool

__all__ = [
    "ORIGINAL_LANG",
    "caption_sidecars",
    "caption_tracks",
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


def video_metadata(url: str, *, executable: str | None = None) -> dict:
    """One yt-dlp metadata read, whole.

    Split out of `caption_tracks` so `mfp transcript` can take the id and the
    title out of the SAME read rather than paying for a second one -- this is
    a platform request, and the budget governor exists because they are not
    free.
    """
    raw = run_tool([
        executable or "yt-dlp", "-J", "--no-download", "--no-warnings", url,
    ])
    return json.loads(raw or "{}")


def caption_tracks(url: str, *, executable: str | None = None
                   ) -> tuple[dict, dict, str | None]:
    """`(manual, automatic, spoken_language)` from one yt-dlp metadata read."""
    return tracks_of(video_metadata(url, executable=executable))


def tracks_of(data: dict) -> tuple[dict, dict, str | None]:
    """The caption half of a metadata read, for a caller that already has one."""
    return (
        data.get("subtitles") or {},
        data.get("automatic_captions") or {},
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
    orig = [k for k in auto if k.endswith("-orig")]
    if orig:
        return orig[0], True
    if language and language in auto:
        return language, True
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
