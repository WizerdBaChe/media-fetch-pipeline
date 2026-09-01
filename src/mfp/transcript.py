"""`mfp transcript` -- the words of a video, as text a person can read.

Nothing here is a new capability. Fetching a caption track, picking the
original language over a machine translation of it, de-duplicating YouTube's
rolling two-line captions, saving the file beside the video so a second run
costs no platform request -- `stack` has done all of that since Phase R. What
it never had was an EXIT: the transcript existed only as an intermediate on
the way to an image, so a user who wanted to read the words had no command to
run and no file to open.

Two things follow from that, and they are the whole design:

1. **This module composes `stack`; it does not reimplement it.** Every
   caption decision stays in one place, because two answers to "which track,
   and where is it cached" is exactly the drift that makes a GUI disagree
   with its CLI.

2. **The output is meant to be chosen FROM.** The default rendering carries a
   timestamp on every line, because the question a reader actually has is not
   "what was said" but "which part of this do I want to quote" -- and the
   answer to that is a `--from`/`--to` window they can hand straight back to
   `mfp stack`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from typing import TYPE_CHECKING

from mfp import asr, runs
from mfp.errors import MfpError, UsageError
from mfp.naming import sanitize_component
from mfp.captions import (
    ORIGINAL_LANG,
    caption_sidecars,
    fetch_captions,
    pick_caption_track,
    tracks_of,
    video_metadata,
)
from mfp.cues import parse_timecode, read_cue_lines

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mfp.config import AsrConfig

__all__ = [
    "FORMATS",
    "RECOGNIZE_MODES",
    "NoCaptionsAvailable",
    "Transcript",
    "TranscriptLine",
    "as_json",
    "available_tracks",
    "clock",
    "load",
    "parse_timecode",
    "render",
    "resolve",
    "summary",
]

#: Rendering styles. `timed` is the default because the reader's real question
#: is which part to quote, and a wall of prose cannot answer it.
FORMATS = ("timed", "text", "srt")

#: When speech recognition may be used.
#:
#: `auto` is the default and the ordering matters: a caption track somebody
#: WROTE beats a machine's guess at the same words, and it also costs
#: seconds instead of minutes. Recognition is what happens when no such
#: track exists -- which for a local mp3 is always.
#:
#: `always` exists for the case the ordering gets wrong: an auto-generated
#: caption track can be worse than a fresh transcription, and the reader can
#: see that and this code cannot.
RECOGNIZE_MODES = ("auto", "always", "never")


class NoCaptionsAvailable(MfpError):
    """The video was read fine and has no caption track at all.

    Exit 3 rather than 5, matching `no_media_in_post`: a video nobody
    captioned is not a parser break, and telling an agent it is would send it
    looking for a code fix that does not exist.
    """

    error_code = "no_captions_available"
    exit_code = 3


@dataclass(frozen=True)
class TranscriptLine:
    """One line, and when it was said. No end time: the next line is the end.

    That is not a simplification, it is what the sources carry. A caption cue
    has an end, but on the rolling-caption path several cues share a line and
    the line's own end is not any one of theirs.
    """

    at: float
    text: str


@dataclass(frozen=True)
class Transcript:
    lines: list[TranscriptLine]
    #: The caption file this was read out of, which is also what `mfp stack
    #: --subs` should be pointed at to quote from it.
    source: Path
    #: "written" | "automatic" | "beside" | "named" -- how the text was
    #: obtained, because a machine transcription and a human one are not the
    #: same evidence and the reader should not have to guess which they have.
    kind: str
    language: str | None
    title: str | None
    start: float
    end: float | None

    @property
    def duration(self) -> float:
        return (self.lines[-1].at - self.lines[0].at) if self.lines else 0.0

    def to_payload(self) -> dict:
        return {
            "source": str(self.source),
            "kind": self.kind,
            "language": self.language,
            "title": self.title,
            "lineCount": len(self.lines),
            "from": self.start,
            "to": self.end,
            "lines": [{"at": round(ln.at, 3), "text": ln.text} for ln in self.lines],
        }


def available_tracks(url: str, *, yt_dlp: str | None = None) -> dict:
    """What caption tracks this video has, without downloading any of them.

    Automatic captions run to a hundred-odd machine translations, so they are
    reported as a COUNT plus the ones that are actually the spoken language.
    Printing all of them buries the two rows that matter.
    """
    data = video_metadata(url, executable=yt_dlp)
    manual, auto, language = tracks_of(data)
    original = sorted(k for k in auto if k.endswith("-orig"))
    return {
        "title": data.get("title"),
        "spokenLanguage": language,
        "written": sorted(manual),
        "automaticOriginal": original,
        "automaticCount": len(auto),
        "automatic": sorted(auto),
    }


def resolve(
    target: str,
    *,
    output_root: str | Path,
    sub_lang: str = ORIGINAL_LANG,
    yt_dlp: str | None = None,
    refresh: bool = False,
    recognize: str = "auto",
    asr_config: "AsrConfig | None" = None,
    asr_language: str = "auto",
    asr_languages: str | None = None,
    say=None,
    on_progress=None,
) -> tuple[Path, str, str | None, str | None]:
    """`(caption_file, kind, language, title)` for whatever the user named.

    Four shapes go in now, in the order a person would try them: a caption
    file they already have, a media file whose captions were saved beside it,
    a media file whose words exist only as SOUND, or a URL. The second one is
    why `stack` and this command share a cache -- a user who built a quote
    image yesterday should not pay a platform request to read the same words
    today.

    The third is the newest and the one with a cost: recognition takes
    minutes where the others take seconds, so it is tried LAST and never
    when a written track is sitting right there.
    """
    say = say or (lambda _m: None)

    if "://" not in target:
        path = Path(target).expanduser()
        if not path.is_file():
            raise UsageError(f"no such file: {path}")
        if path.suffix.lower() in (".srt", ".vtt", ".txt"):
            return path, "named", None, None

        if recognize != "always":
            beside = caption_sidecars(path)
            if beside:
                say(f"reading the captions already beside the video: {beside[0].name}")
                return beside[0], "beside", _lang_of(beside[0]), path.stem

        if recognize == "never":
            raise UsageError(
                f"no caption file beside {path.name}, and --recognize never "
                f"forbids listening to it. Pass the post URL instead, or drop "
                f"the flag to transcribe the audio"
            )
        if not asr.is_media_file(path):
            raise UsageError(
                f"no caption file beside {path.name}, and {path.suffix or 'that'} "
                f"is not a container this can listen to. Pass a media file, a "
                f"caption file (.srt/.vtt/.txt), or the post URL"
            )
        return _recognize_local(
            path,
            output_root=output_root,
            refresh=refresh,
            asr_config=asr_config,
            asr_language=asr_language,
            asr_languages=asr_languages,
            say=say,
            on_progress=on_progress,
        )

    data = video_metadata(target, executable=yt_dlp)
    manual, auto, language = tracks_of(data)
    if not manual and not auto:
        raise NoCaptionsAvailable(
            "that video has no caption track at all -- not written, not "
            "automatic. If the words are burned into the picture, "
            "`mfp stack --roi` reads those instead"
        )

    key, is_auto = pick_caption_track(manual, auto, language, sub_lang)
    title = data.get("title")
    stem = sanitize_component(title or data.get("id"), fallback="unknown_video")

    existing = runs.find(output_root, target)
    if existing and not refresh:
        cached = existing.captions(key)
        if cached:
            say(f"already have these captions: {cached[0].name} (--refresh to re-fetch)")
            return cached[0], "automatic" if is_auto else "written", key, title

    run = runs.open_run(output_root, target, stem=stem, kind="url", verb='transcript')
    say(f"fetching {'automatic' if is_auto else 'written'} {key} captions")
    found = fetch_captions(
        target, key, is_auto, run.subtitles, stem=stem, executable=yt_dlp
    )
    runs.record_transcript(run, found, key)
    say(f"saved: {found}")
    return found, "automatic" if is_auto else "written", key, title


def _recognize_local(
    path: Path,
    *,
    output_root: str | Path,
    refresh: bool,
    asr_config: "AsrConfig | None",
    asr_language: str,
    asr_languages: str | None,
    say,
    on_progress,
) -> tuple[Path, str, str | None, str | None]:
    """Listen to a media file, and cache the words the way a fetch is cached.

    The recognised text lands in its own run folder under
    `<outputRoot>/逐字稿/` rather than beside the user's file, which is a
    deliberate asymmetry with the caption path: a video this product
    downloaded is ours to put a sidecar next to, and an mp3 the user picked
    from anywhere is not. Our own output root is also always writable, which
    a folder on a read-only share is not.
    """
    from mfp.config import AsrConfig

    settings = asr_config or AsrConfig()
    stem = asr.clean_stem(path)

    existing = runs.find(output_root, path)
    if existing and not refresh:
        cached = existing.captions()
        if cached:
            say(f"already transcribed this file: {cached[0].name} (--refresh to redo)")
            return cached[0], asr.RECOGNIZED, _lang_of(cached[0]), path.stem

    from mfp.asr_models import readiness, resolve_model_argument

    runtime = asr.find_runtime(settings.python)
    if runtime is None:
        # The one sentence the caller needs, taken from the same readiness
        # check the GUI renders, so the two surfaces cannot drift apart --
        # they used to say different things about the same machine.
        verdict = readiness(settings, output_root)
        recognition = verdict.capability("recognition")
        steps = " ".join(step.text for step in (recognition.steps if recognition else []))
        raise asr.AsrUnavailable(
            f"{recognition.detail if recognition else ''} {steps}".strip()
            + f"（設定 asr.python，或環境變數 {asr.ENV_PYTHON}；`mfp asr-status` 會說明現況）"
        )

    model, model_dir = resolve_model_argument(settings, output_root)
    # The model that will ACTUALLY load, not the one the config asks for.
    # A local folder resolves to an absolute path, and printing the
    # configured name beside it would let `large-v3` describe a run that
    # loaded something else entirely -- the settings panel names the folder,
    # so this does too.
    say(
        "no captions for this file -- listening to it "
        f"({Path(model).name if model_dir is None else model})"
    )

    # Created only now. A run folder that exists is a claim that an analysis
    # happened, and everything above this line can still refuse to start one.
    run = runs.open_run(output_root, path, stem=stem, kind="media", verb='transcript')
    outcome = asr.recognize(
        path,
        out_dir=run.root,
        stem=stem,
        python_exe=runtime,
        model=model,
        model_dir=model_dir,
        device=settings.device,
        compute_type=settings.compute_type,
        language=asr_language,
        languages=asr_languages,
        script=settings.script,
        audio=settings.audio,
        allow_download=settings.allow_download,
        say=say,
        on_progress=on_progress,
    )
    runs.record_transcript(run, outcome.source, outcome.language)
    return outcome.source, asr.RECOGNIZED, outcome.language, path.stem


def _lang_of(path: Path) -> str | None:
    """`video.zh-Hant.srt` -> `zh-Hant`. yt-dlp puts the language between the
    stem and the extension, which is the only place it is recorded."""
    parts = path.name.rsplit(".", 2)
    return parts[-2] if len(parts) == 3 else None


def load(
    target: str,
    *,
    output_root: str | Path,
    sub_lang: str = ORIGINAL_LANG,
    start: float = 0.0,
    end: float | None = None,
    yt_dlp: str | None = None,
    refresh: bool = False,
    recognize: str = "auto",
    asr_config: "AsrConfig | None" = None,
    asr_language: str = "auto",
    asr_languages: str | None = None,
    say=None,
    on_progress=None,
) -> Transcript:
    source, kind, language, title = resolve(
        target,
        output_root=output_root,
        sub_lang=sub_lang,
        yt_dlp=yt_dlp,
        refresh=refresh,
        recognize=recognize,
        asr_config=asr_config,
        asr_language=asr_language,
        asr_languages=asr_languages,
        say=say,
        on_progress=on_progress,
    )
    raw = source.read_text(encoding="utf-8", errors="replace")
    lines, _is_transcript = read_cue_lines(
        raw, start, end if end is not None else float("inf")
    )
    if not lines:
        window = f" between {clock(start)} and {clock(end)}" if end is not None else ""
        raise NoCaptionsAvailable(
            f"the caption file holds no lines{window}: {source}"
        )
    return Transcript(
        lines=[TranscriptLine(at, text) for at, text in lines],
        source=source,
        kind=kind,
        language=language,
        title=title,
        start=start,
        end=end,
    )


def clock(seconds: float | None) -> str:
    """`h:mm:ss` past an hour, `m:ss` under it -- the shape `--from` accepts,
    so a line the reader picked can be pasted straight back."""
    if seconds is None:
        return "-"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def render(transcript: Transcript, fmt: str = "timed", *, width: int = 0) -> str:
    """The transcript as text. `srt` hands back the file itself, unchanged --
    anything else would be a second dialect of a format that already has one."""
    if fmt not in FORMATS:
        raise UsageError(f"unknown --format {fmt!r}; one of: {', '.join(FORMATS)}")

    if fmt == "srt":
        return transcript.source.read_text(encoding="utf-8", errors="replace")

    if fmt == "timed":
        return "\n".join(f"[{clock(ln.at)}] {ln.text}" for ln in transcript.lines)

    # `text`: paragraphs rather than one line per caption. The builder moved
    # to `asr.to_text` when recognition began writing a `.txt` beside its
    # `.srt` -- the file on disk and the text printed here are the same
    # document, and keeping two copies of "where does a paragraph end" is
    # how they would stop being the same document.
    return asr.to_text((line.text for line in transcript.lines), width=width)


def summary(transcript: Transcript) -> str:
    """The one line printed to stderr beside the text, so the reader knows
    what they are looking at without it polluting stdout."""
    what = {
        "written": "human-written captions",
        "automatic": "automatic (machine) captions",
        "beside": "the caption file beside the video",
        "named": "the caption file you named",
        # Named for what it IS. "transcript" would be circular, and
        # "speech recognition" would let a reader forget that no human
        # checked a word of it.
        asr.RECOGNIZED: "listening to the audio (machine transcription)",
    }.get(transcript.kind, transcript.kind)
    span = f"{clock(transcript.lines[0].at)}–{clock(transcript.lines[-1].at)}"
    lang = f" [{transcript.language}]" if transcript.language else ""
    return f"{len(transcript.lines)} lines, {span}, from {what}{lang}"


def as_json(transcript: Transcript) -> str:
    return json.dumps({"transcript": transcript.to_payload()}, ensure_ascii=False)
