"""Speech recognition as an EXTERNAL tool, on the far side of a process.

`mfp transcript` has always been able to read words that somebody else had
already written down -- a platform's caption track, a `.srt` beside a video,
a file the user named. What it could not do is read words that exist only as
sound, which is the entire content of an mp3, a voice memo, or a recording of
a meeting. This module is that missing half.

**It is not a dependency of this package.** faster-whisper, CTranslate2 and
their CUDA runtime are ~400 MB of Python plus a ~3 GB model; the product's
installer is ~107 MB and the rule this project has followed since Phase G is
that a new Python dependency IS installer size. So the engine sits in its own
interpreter, `mfp` invokes it the way it invokes yt-dlp and ffmpeg, and the
installer does not change. `asr/runner.py` is the script on the other side and
the only thing that imports faster-whisper anywhere in this repository.

**The result is written as a `.srt` and then read back.** That looks like a
detour and is the opposite: every other transcript source arrives as a caption
file, and `read_cue_lines` is the one parser that turns any of them into lines.
Handing recognition its own in-memory path would give this product a second
answer to "what is a transcript line", and the two would drift. Writing the
file also means a re-read costs nothing and that `引用長圖` can quote a
recognised transcript through `--subs` with no special case at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from mfp import quality
from mfp.errors import MfpError
from mfp.naming import sanitize_component

__all__ = [
    "AUDIO_SUFFIXES",
    "MEDIA_SUFFIXES",
    "RECOGNIZED",
    "AsrOutcome",
    "AsrUnavailable",
    "clean_stem",
    "find_runtime",
    "find_runtime_detailed",
    "fetch_model_path",
    "is_media_file",
    "recognize",
    "run_sidecar",
    "runner_path",
    "to_srt",
    "to_text",
]


class AsrUnavailable(MfpError):
    """No engine on this machine -- not a failure of the file or the run.

    Defined here rather than in `errors.py` for the same reason
    `NoCaptionsAvailable` lives in `transcript.py`: it is raised and caught
    as an exception by one verb and never travels as an `errorCode` string
    through a `FetchResult`, which is the only path `exit_code_for` serves.

    Exit 6, joining `dependency_missing`: from the caller's side "yt-dlp is
    not installed" and "the recognition engine is not installed" are the
    same sentence with a different noun, and an agent that knows what to do
    about one knows what to do about the other.
    """

    error_code = "asr_unavailable"
    exit_code = 6

#: The `kind` a recognised transcript carries, joining `written`,
#: `automatic`, `beside` and `named`. It exists for the same reason those
#: four do: a machine listening to audio and a human typing captions are not
#: the same evidence, and the reader must not have to guess which they have.
RECOGNIZED = "recognized"

#: Audio-only containers. The list is an ADVERTISEMENT, not a gate -- the
#: real answer to "can this be opened" is FFmpeg's, and the runner asks it.
#: Anything here is known to work; something not here may still work, and is
#: refused only by the decoder actually failing.
AUDIO_SUFFIXES = frozenset({
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".caf", ".amr", ".mka", ".m4b", ".wv",
})

#: Containers that may carry an audio track alongside pictures. `.m4v` and
#: `.mov` are here because an iPhone writes both.
VIDEO_SUFFIXES = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".3gp",
})

MEDIA_SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES

#: The runner's exit code for "this environment cannot run recognition",
#: kept distinct from 1 ("that file could not be read") so the two produce
#: different advice. Mirrors `EXIT_UNUSABLE` in `asr/runner.py`.
EXIT_UNUSABLE = 2

#: Interpreters to try when the config names none. Order is deliberate: a
#: venv this project owns beats one borrowed from a neighbouring project,
#: because the neighbour is free to delete it.
_CANDIDATE_VENVS = (
    Path(".asr-venv") / "Scripts" / "python.exe",
    Path(".asr-venv") / "bin" / "python",
)

#: Environment override, for a machine where the engine lives somewhere no
#: rule would guess. Same escape hatch `binaries` gives yt-dlp.
ENV_PYTHON = "MFP_ASR_PYTHON"

#: Where a configured interpreter came from, in the words the setup panel
#: uses. The ORDER these are searched is a decision (see `find_runtime`);
#: naming the winner is what lets a user understand why the path they just
#: typed into settings is being ignored in favour of a shell variable.
RuntimeSource = str  # "environment" | "settings" | "beside-the-app" | None


def _search_roots() -> list[Path]:
    """Directories a `.asr-venv` may sit next to.

    The checkout root has always been searched. The INSTALL directory is new
    and is the one that matters for a packaged build: `Path(__file__)` under
    PyInstaller lands inside the `%TEMP%` unpack directory, so the old search
    could never find anything on the machines this product actually ships to.
    Somebody who drops an engine venv beside the app now gets it found.
    """
    roots: list[Path] = []
    try:
        from mfp.asr_models import _install_dir  # local: see asr_models' header

        installed = _install_dir()
        if installed is not None:
            roots.append(installed)
    except Exception:  # noqa: BLE001 - discovery must never break a run
        pass
    if not getattr(sys, "frozen", False):
        roots.append(Path(__file__).resolve().parents[2])
    return roots


def find_runtime_detailed(
    configured: str | None = None, *, root: Path | None = None
) -> tuple[Path | None, RuntimeSource | None, str | None]:
    """`(interpreter, where_it_came_from, what_went_wrong)`.

    The single implementation of the search order; `find_runtime` is the
    one-value view of it. Split apart because a setup panel has to be able
    to say "the environment variable names a file that is not there" --
    which is a completely different instruction from "nothing is configured"
    and was previously indistinguishable from it.
    """
    from_env = os.environ.get(ENV_PYTHON)
    if from_env:
        candidate = Path(from_env).expanduser()
        if candidate.is_file():
            return candidate, "environment", None
        return None, "environment", (
            f"環境變數 {ENV_PYTHON} 指到 {candidate}，但那裡沒有這個檔案。"
        )

    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate, "settings", None
        return None, "settings", (
            f"設定裡指定的辨識引擎是 {candidate}，但那裡沒有這個檔案。"
        )

    roots = [root] if root is not None else _search_roots()
    for base in roots:
        for relative in _CANDIDATE_VENVS:
            candidate = base / relative
            if candidate.is_file():
                return candidate, "beside-the-app", None
    return None, None, None


@dataclass(frozen=True)
class AsrOutcome:
    """Where the words landed, and what produced them."""

    #: The `.srt` written. Everything downstream reads THIS, not the JSON.
    source: Path
    language: str | None
    #: `{"name", "model", "device", "computeType", "realtimeFactor", ...}`,
    #: reported so a slow run can be explained without re-running it.
    engine: dict
    duration: float
    #: What the engine noticed about its OWN output: how many passages it had
    #: to retry, how many of those threw the running context away, the longest
    #: run of identical lines, and the peak memory it took. Empty for an
    #: engine build that predates the field, which is why every reader uses
    #: `.get`. A transcript is a file either way -- this is the difference
    #: between handing one over and being able to say what it is worth.
    health: dict = field(default_factory=dict)
    #: What WE could determine about the text, from `mfp.quality`.
    #:
    #: Deliberately separate from `health`, and the separation is the point.
    #: `health` is a black box's report on itself and can only be believed;
    #: `findings` are properties of the file, re-derivable by anyone holding
    #: it. When the two disagree, the second one is the evidence.
    findings: list = field(default_factory=list)
    #: Which language the engine decoded each passage in, as
    #: `[{"language", "start", "end", "windows", "agreement"}, ...]`.
    #:
    #: A first-class result rather than a detail of the run, because it is
    #: what `mfp.quality` rules the text against and what a reader needs in
    #: order to know which minutes of a bilingual recording to trust. Empty
    #: for a caption track, a hand-written file, or an engine build that
    #: predates it -- every reader uses `.get`/truthiness for that reason.
    plan: list = field(default_factory=list)


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_SUFFIXES


def runner_path() -> Path:
    """`asr/runner.py`, in a checkout and in a frozen build.

    Same shape as `mfp.agent.skill_path()` and for the same reason: the
    walk-up-from-`__file__` answer is correct in a source tree and lands
    inside the PyInstaller unpack directory in a packaged one. `mfp.spec`
    ships this file to `asr/runner.py` under the bundle root; if that entry
    is ever dropped, `recognize` fails naming this path rather than
    reporting that the engine is missing.
    """
    return _beside_runner("runner.py")


def fetch_model_path() -> Path:
    """`asr/fetch_model.py`, the downloader, found the same way.

    A second script rather than a mode of the first: `runner.py` imports
    faster-whisper and loads a 3 GB model to do anything at all, and asking
    it to also be the thing that FETCHES that model would mean the download
    path could only run on a machine that already had the download.
    """
    return _beside_runner("fetch_model.py")


def _beside_runner(name: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "asr" / name
    return Path(__file__).resolve().parents[2] / "asr" / name


def find_runtime(configured: str | None = None, *, root: Path | None = None) -> Path | None:
    """The Python that can run the engine, or `None`.

    Three sources, NARROWEST SCOPE FIRST: the environment, the config, a
    venv this project owns.

    The environment beating the config is the whole of the ordering
    decision, and it was the other way round until it was tried: with
    `asr.python` set, exporting `MFP_ASR_PYTHON` did nothing at all and said
    nothing about it -- a switch that silently does not switch. A variable
    is set for one shell and one run; a config file is the standing answer,
    and the standing answer is what an override is for.

    Deliberately NOT `sys.executable`: in a packaged build that is
    `mfp.exe`, which is not a Python interpreter at all, and in a checkout
    it is the project venv, which does not have faster-whisper and must not
    be silently expected to.

    A named-but-absent interpreter returns `None` rather than falling
    through to the next source. Running the engine from somewhere the user
    did not name, and reporting success for it, is worse than saying no.

    The search itself lives in `find_runtime_detailed`, which also reports
    WHICH source won and why a named one was rejected; this function is the
    answer callers that only need the path have always asked for.
    """
    return find_runtime_detailed(configured, root=root)[0]


def clean_stem(media: Path) -> str:
    """The base name this file's outputs carry. Readable, and nothing else.

    It used to end in eight hex characters of `sha1(resolved path)`, because
    the cache was a flat folder where two `recording.mp3` from two
    directories would otherwise collide -- and a collision there is a WRONG
    transcript that looks exactly like a right one. The digest is gone
    because the flat folder is: `mfp.runs` gives every analysis its own
    folder and keys the cache on `_source.json`, so identity no longer has to
    be smuggled through the file name (user ruling 2026-08-28).
    """
    return sanitize_component(media.stem, fallback="audio")


def _timestamp(seconds: float) -> str:
    """`HH:MM:SS,mmm` -- SubRip's format, which is not negotiable."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def to_srt(segments: list[dict]) -> str:
    """Recognised segments as SubRip text.

    Written rather than borrowed because nothing in this project wrote SRT
    before -- `stack` only ever read it. Two properties are what a
    downstream parser can actually be broken by, and both are enforced here
    rather than assumed of the engine:

    - **The cue numbers are consecutive.** Numbered by INPUT position until
      2026-08-27, which skipped one every time a blank segment was dropped
      (`1, 3, 4`). SubRip's own spec numbers them sequentially and several
      players treat a gap as a truncated file.
    - **An end never precedes its start.**
    """
    blocks = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if end < start:
            end = start
        blocks.append(
            f"{len(blocks) + 1}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n"
        )
    return "\n".join(blocks)


#: Where a caption line is allowed to end a paragraph. The closing quote is
#: optional and follows the stop, because 「……好。」 ends a sentence and the
#: bare period test would leave the bracket dangling into the next one.
_SENTENCE_END = re.compile(r"[.!?。！？…]['\"”』」)]?$")


#: Characters that do not want a space beside them. CJK, fullwidth
#: punctuation, and the ideographic space -- everything whose writing system
#: has no word separator to begin with.
_CJK = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿"
    r"豈-﫿︰-﹏＀-･]"
)

#: The paragraph length the `.txt` falls back to when the transcript has no
#: sentence punctuation to break on.
#:
#: Not a preference. With the voice-activity filter off -- which is the rung
#: that rescues a quiet recording -- the engine returns segments that mostly
#: end without a full stop, so sentence-end breaking finds nothing and 158
#: lines join into one 4,000-character paragraph (measured 2026-08-28 on the
#: UAT recording). A wall of text is not a reading copy.
TEXT_PARAGRAPH_WIDTH = 120

#: Below this share of lines ending in sentence punctuation, a transcript has
#: no sentences to reflow around and the engine's own segment boundaries are
#: kept instead.
#:
#: Measured on the UAT recording, three ways (2026-08-28):
#:
#:     filter on  (rung 0)        4 lines, 75% end in a full stop
#:     filter OFF (rung 1)      158 lines,  0%
#:     dynaudnorm + filter on   125 lines, 100%
#:
#: The two behaviours are that far apart, so any line between them would do
#: -- which is the argument for a threshold rather than against one. A
#: quarter is chosen to sit well clear of the 0% case in case a future rung
#: punctuates a little.
SENTENCE_SHARE_FLOOR = 0.25


def _join(left: str, right: str) -> str:
    """Two caption lines, with a space only where a space belongs.

    `" ".join` is right for English and wrong for Chinese: a caption break
    falls wherever the screen ran out of room, and joining 「第二句話開始」 to
    「然後結束了」 with a space invents a word boundary the speaker did not
    say. Decided by the characters actually meeting at the seam, so a
    code-switched line -- 「先 review 這個」 -- keeps the spaces its English
    needs.
    """
    if not left:
        return right
    # BOTH sides, not either. Chinese meeting Chinese needs no separator;
    # Chinese meeting English does, and 「先 review 這個」 is the normal
    # register in the recordings this product is pointed at.
    if _CJK.search(left[-1]) and _CJK.search(right[0]):
        return left + right
    return f"{left} {right}"


def to_text(texts: Iterable[str], *, width: int = 0) -> str:
    """Caption lines as PARAGRAPHS -- the plain-reading form of a transcript.

    Lives here, beside `to_srt`, because this is the second thing this
    project turns recognised segments into and the two must stay one
    decision. `transcript.render(fmt="text")` calls it rather than keeping
    its own copy: a transcript read on the command line and a `.txt` written
    beside the `.srt` are the same document, and two implementations of
    "where does a paragraph end" would drift apart the first time either was
    touched.

    A caption's line breaks are a function of the screen it was written for
    and mean nothing on the page, so they are dropped and rebuilt: broken at
    sentence ends, and at whatever `width` asks for.
    """
    lines = [text for text in (str(raw).strip() for raw in texts) if text]
    if not lines:
        return ""

    # Does this transcript HAVE sentences? Asked rather than assumed, because
    # the answer differs by rung: with the voice-activity filter on the
    # engine punctuates, and with it off -- the rung that rescues a quiet
    # recording -- it mostly does not. Gluing unpunctuated breath groups into
    # 120-character blocks produces something harder to read than the `.srt`
    # this file exists to be an alternative to. When there is no punctuation
    # to reflow around, the engine's own segment boundaries are the best
    # structure available, so they are kept.
    ended = sum(1 for line in lines if _SENTENCE_END.search(line))
    if ended < len(lines) * SENTENCE_SHARE_FLOOR:
        return "\n".join(lines)

    out: list[str] = []
    joined = ""
    for line in lines:
        joined = _join(joined, line)
        if _SENTENCE_END.search(line) or (width and len(joined) >= width):
            out.append(joined)
            joined = ""
    if joined:
        out.append(joined)
    return "\n\n".join(out)


#: The rungs, in the order they are climbed.
#:
#: Rung 0 is what every build before this one did, unchanged: the engine's
#: own voice-activity filter on, because silence is where Whisper
#: hallucinates and that reason has not stopped being true.
#:
#: Rung 1 turns it off, and exists because the same filter DELETES SPEECH on
#: a recording where one voice is much quieter than the other. Measured on a
#: two-person call (UAT 2026-08-28): silero kept 37.1s of 326.5s -- the
#: entire far side of the conversation classified as non-speech -- and the
#: engine, handed 11% of a discussion, produced four fluent lines. With the
#: filter off, the same file, same model, same machine: 158 segments, twice,
#: to the segment, with no findings at all.
#:
#: Only ONE extra rung, and not the third and fourth that were measured.
#: Level normalisation (`dynaudnorm`) also rescues the VAD -- it takes the
#: retained share from 11% to 98% -- and then produces 125-140 segments where
#: this produces 158, at 8.6x realtime rather than 15x, for the price of a
#: whole new ffmpeg stage. Denoising is worse than doing nothing: `afftdn`
#: took the retained share DOWN, to 9%, because the quiet speaker is exactly
#: what a denoiser removes. A rung that buys nothing measurable does not get
#: built.
_LADDER = (
    {"rung": 0, "argv": (), "vadFilter": True,
     "why": None},
    {"rung": 1, "argv": ("--no-vad",), "vadFilter": False,
     "why": "這份稿子看起來漏掉了內容，改用「不過濾人聲」重跑一次…"},
)


def _judge(payload: dict, *, script: str) -> list[quality.Finding]:
    """Everything determinable about one attempt, worst first.

    Both halves, always. `inspect_cues` reads the text; `inspect_run` reads
    the engine's account of its own work -- and only the second one can see
    deletion, because words removed before the decoder ran leave a transcript
    that is fluent, punctuated and complete-looking.
    """
    segments = payload.get("segments") or []
    engine = payload.get("engine") or {}
    findings = quality.inspect_cues(
        segments,
        duration=float(payload.get("duration") or 0.0) or None,
        language=payload.get("language"),
        expect_script=script,
        # What the engine was ACTUALLY told, so the echo check rules on the
        # real instruction rather than on a copy kept over here that would
        # go stale the first time the runner's wording changed.
        instruction=engine.get("instruction"),
        # ...and which language it decoded each passage in, for the same
        # reason. Without it `inspect_cues` can only ask questions about the
        # text; with it, it can ask whether the text is what the engine says
        # it produced -- which is the one shape that catches a whole passage
        # decoded in the wrong language.
        plan=payload.get("languagePlan") or None,
    )
    findings += quality.inspect_run(payload.get("health") or {},
                                    segments=len(segments))
    return sorted(
        findings,
        key=lambda f: ({"warn": 0, "note": 1}.get(f.severity, 9), f.at or 0.0),
    )


#: Warnings the ladder must NOT climb for, because no rung on it addresses
#: them. The ladder's only rung turns voice-activity filtering off, which
#: exists for one failure: the VAD deleting a quiet speaker. A transcript
#: decoded in the wrong language, or one where the standing instruction
#: replaced a passage, is not made better by changing the VAD -- and a ladder
#: that climbs anyway doubles the cost of every such run to arrive at the
#: same answer. Named rather than inferred, so adding a finding is a decision
#: about whether a retry could possibly help it.
_NOT_A_LADDER_PROBLEM = frozenset({"language-drift", "instruction-capture"})


def _alarming(payload: dict, findings: list[quality.Finding]) -> bool:
    """Is this attempt bad enough to be worth spending another one on?

    A `warn` the ladder could actually act on, or nothing at all. `note`-level
    findings are true things about an honest transcript and must not start a
    second run -- a ladder that climbs on every recording is a ladder that
    doubles the cost of the product for no one.
    """
    return not (payload.get("segments") or []) or any(
        f.severity == "warn" and f.code not in _NOT_A_LADDER_PROBLEM
        for f in findings
    )


def _better(challenger: tuple, incumbent: tuple) -> tuple:
    """Which attempt to keep. `(payload, findings, rung)` on both sides.

    The challenger has to EARN the swap: strictly fewer warnings, or the
    incumbent had nothing at all. A tie keeps rung 0, and that is the whole
    protection for the case this ladder must not break -- half an hour of
    ambience produces a stuck, repeating transcript with the filter on and a
    longer stuck, repeating transcript with it off. Both raise one warning,
    neither is better, so the original stands and the retry has cost the
    reader nothing but time.
    """
    c_payload, c_findings, _ = challenger
    i_payload, i_findings, _ = incumbent
    if not (c_payload.get("segments") or []):
        return incumbent
    if not (i_payload.get("segments") or []):
        return challenger
    c_warns = sum(1 for f in c_findings if f.severity == "warn")
    i_warns = sum(1 for f in i_findings if f.severity == "warn")
    return challenger if c_warns < i_warns else incumbent


def recognize(
    media: Path,
    *,
    out_dir: Path,
    python_exe: Path,
    stem: str | None = None,
    model: str = "large-v3",
    model_dir: str | None = None,
    device: str = "auto",
    compute_type: str = "auto",
    language: str = "auto",
    languages: str | None = None,
    script: str = "trad",
    audio: str = "none",
    allow_download: bool = False,
    retry: bool = True,
    say=None,
    on_progress=None,
) -> AsrOutcome:
    """Run the engine over one file and leave a `.srt` in `out_dir`.

    `out_dir` is a run folder (`mfp.runs`), so the `.srt` lands in its
    字幕檔 and the reading copy in its 文字檔. `stem` is the base name both
    get; it defaults to the media file's own, sanitized.

    `say` receives the engine's human lines as they arrive, so a five-minute
    transcription is not five minutes of silence. `on_progress` receives the
    parsed `@asr` records for anything that wants a bar rather than prose.

    `retry` climbs `_LADDER` when the first attempt fails its own inspection.
    Off is for a caller that must not pay for a second pass; the transcript
    it then gets is the one every build before this returned, findings and
    all.
    """
    say = say or (lambda _m: None)
    runner = runner_path()
    if not runner.is_file():
        raise MfpError(
            f"the packaged speech-recognition runner is missing at {runner}"
        )

    def attempt(step: dict) -> dict:
        return _run_engine(
            media, runner=runner, python_exe=python_exe, model=model,
            model_dir=model_dir, device=device, compute_type=compute_type,
            language=language, languages=languages, script=script,
            audio=audio, allow_download=allow_download,
            extra=list(step["argv"]), say=say, on_progress=on_progress,
        )

    first = _LADDER[0]
    payload = attempt(first)
    findings = _judge(payload, script=script)
    best = (payload, findings, first)

    if retry and _alarming(payload, findings):
        for step in _LADDER[1:]:
            say(step["why"])
            if on_progress is not None:
                on_progress({"phase": "retry", "rung": step["rung"],
                             "reason": step["why"]})
            try:
                challenger = attempt(step)
            except AsrUnavailable:
                # The engine was there a moment ago, so this is about THIS
                # run, not about the machine. Keep what we already have.
                break
            except MfpError as exc:
                say(f"note: 重跑沒有成功（{exc}），沿用第一次的結果")
                break
            best = _better(
                (challenger, _judge(challenger, script=script), step), best
            )
            if not _alarming(best[0], best[1]):
                break

    payload, findings, step = best
    if step["rung"] != first["rung"]:
        say(f"用的是第 {step['rung']} 階的結果（不過濾人聲）")

    segments = payload.get("segments") or []
    if not segments:
        raise MfpError(
            f"the engine heard no speech in {media.name}. If the file is "
            f"music or silence that is the right answer; if it is not, try "
            f"--asr-lang to name the language instead of detecting it"
        )

    from mfp import runs

    out_dir.mkdir(parents=True, exist_ok=True)
    language_tag = payload.get("language") or "und"
    base = stem or sanitize_component(media.stem, fallback="audio")
    target = runs.place(out_dir, f"{base}.{language_tag}.srt")
    target.write_text(to_srt(segments), encoding="utf-8")
    say(f"saved: {target}")

    # ...and the same words as a document (UAT 2026-08-28). An `.srt` is a
    # file format for a player, and a reader who wants to READ the
    # transcript was being handed cue numbers and timecodes. Both are
    # written because they are for different things and neither replaces the
    # other: the `.srt` is what 引用長圖 and every caption parser reads, the
    # `.txt` is what a person opens.
    #
    # Written UNCONDITIONALLY rather than on request, because the cost is a
    # few kilobytes and the alternative is a button that has to be found.
    # It cannot be mistaken for the caption track on the resume path for two
    # reasons now: it is in the other subfolder, and `_source.json` names the
    # `.srt` as the transcript. Either one alone would do; the failure being
    # prevented is handing the reader a transcript with no timeline at all.
    plain = runs.place(out_dir, f"{base}.{language_tag}.txt")
    plain.write_text(
        to_text((str(s.get("text", "")) for s in segments),
                width=TEXT_PARAGRAPH_WIDTH) + "\n",
        encoding="utf-8",
    )
    say(f"saved: {plain}")

    # The language plan, beside the words it explains, and ONLY when the file
    # carried more than one language. A single-language recording gets no
    # extra file, because a plan that says "all of it was Chinese" is what the
    # transcript's own name already says.
    #
    # Written as evidence rather than as configuration: it carries the raw
    # per-window votes as well as the smoothed stretches, because a stretch
    # published without the votes behind it is a verdict nobody can check.
    plan = payload.get("languagePlan") or []
    if len({s.get("language") for s in plan}) > 1:
        record = runs.place(out_dir, f"{base}.{language_tag}.language.json")
        record.write_text(
            json.dumps({
                "schemaVersion": 1,
                "transcript": target.name,
                "windowSeconds": 30.0,
                "heard": payload.get("languagesHeard") or {},
                "stretches": plan,
                "windows": payload.get("languageMap") or [],
            }, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        say(f"saved: {record}")
        say("這份錄音裡不只一種語言，各段用的語言記在上面那個檔案裡。")

    for finding in findings:
        say(("WARNING: " if finding.severity == "warn" else "note: ") + finding.detail)
    if on_progress is not None and findings:
        # Onto the machine's channel too. The server forwards progress to the
        # event stream and drops `say` entirely, so a finding that only went
        # to stderr would never reach the window.
        on_progress({
            "phase": "findings",
            "findings": [
                {"code": f.code, "severity": f.severity, "detail": f.detail,
                 "at": f.at, "evidence": f.evidence}
                for f in findings
            ],
        })

    engine = dict(payload.get("engine") or {})
    # Which rung produced this. Without it, two transcripts of the same file
    # made under different conditions are indistinguishable after the fact --
    # and one of them is the one that had to be rescued.
    engine["rung"] = step["rung"]
    engine["vadFilter"] = step["vadFilter"]
    return AsrOutcome(
        source=target,
        language=payload.get("language"),
        engine=engine,
        duration=float(payload.get("duration") or 0.0),
        health=payload.get("health") or {},
        findings=findings,
        plan=plan,
    )


def _run_engine(
    media: Path,
    *,
    runner: Path,
    python_exe: Path,
    model: str,
    model_dir: str | None,
    device: str,
    compute_type: str,
    language: str,
    languages: str | None,
    script: str,
    audio: str,
    allow_download: bool,
    extra: list[str],
    say,
    on_progress,
) -> dict:
    """One invocation of the engine, start to validated payload.

    Split out of `recognize` when the ladder arrived, because a retry has to
    run exactly what the first attempt ran apart from the flags being tested.
    Two copies of this spawn would be two chances to fix the pipe-draining
    deadlock in only one of them.
    """
    argv = [
        str(python_exe), str(runner), str(media),
        "--model", model,
        "--device", device,
        "--compute-type", compute_type,
        "--language", language,
        "--script", script,
        "--audio", audio,
        *extra,
    ]
    if languages:
        argv += ["--languages", languages]
    if model_dir:
        argv += ["--model-dir", model_dir]
    if allow_download:
        argv.append("--allow-download")

    return run_sidecar(argv, say=say, on_progress=on_progress)


def run_sidecar(argv: list[str], *, say=None, on_progress=None) -> dict:
    """Spawn one `asr/` script and return its validated payload.

    Every script on the far side of this boundary speaks the same contract --
    `@asr` records and human lines on stderr, one JSON object on stdout --
    so they share the machinery that reads it. That is not tidiness: the
    pipe-draining below is the fix for a deadlock that made transcription
    hang forever on anything over ~40 segments, and a second copy of this
    would be a second place for that fix to be missing.
    """
    say = say or (lambda _m: None)

    process = subprocess.Popen(
        argv,
        # The engine reads nothing from stdin, and it must not INHERIT ours:
        # the packaged sidecar runs with `--exit-on-stdin-eof`, whose watcher
        # thread sits blocked on our stdin handle, and a child that inherits
        # it hangs for ten seconds and then reports a failure that has
        # nothing to do with the work. Measured on `mfp doctor` 2026-08-17;
        # `test_subprocess_stdin.py` is what keeps every spawn site honest.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # No shell, and no window: a console flashing up mid-run in the
        # desktop app is indistinguishable from a crash to the person
        # watching. CREATE_NO_WINDOW exists only on Windows.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # BOTH pipes must be drained AS THEY ARRIVE, and stderr alone is not
    # enough. The comment here used to say stderr, and it was half the story:
    #
    #   - stderr fills because a long transcription prints one progress line
    #     per segment.
    #   - stdout fills because the RESULT is one JSON object holding every
    #     segment, and on Windows an anonymous pipe holds only a few KB.
    #
    # Draining stderr to EOF and only then reading stdout deadlocks on the
    # second one: stderr closes when the child exits, the child cannot exit
    # until its stdout write completes, and the write cannot complete until
    # somebody reads. Measured 2026-08-28 -- 20 segments finished, 40 hung
    # forever. That is under two minutes of speech, so this feature only ever
    # worked on clips, and the 36-minute file that exposed it sat at 0% GPU
    # for fifteen minutes looking exactly like a slow model.
    #
    # So stdout gets its own thread. It reads to EOF and stops; there is no
    # ordering to preserve between the two pipes, because stdout carries one
    # object and stderr carries everything else.
    assert process.stdout is not None
    assert process.stderr is not None
    collected: list[str] = []

    def _drain_stdout() -> None:
        collected.append(process.stdout.read())

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()

    for raw in process.stderr:
        line = raw.rstrip("\r\n")
        if line.startswith("@asr "):
            if on_progress is not None:
                try:
                    on_progress(json.loads(line[5:]))
                except json.JSONDecodeError:
                    pass
            continue
        if line:
            say(line)

    reader.join()
    process.wait()
    payload = _last_json_object("".join(collected))

    # A COMPLETE result outranks the exit code, and only a complete one.
    #
    # The engine writes its result as one flushed JSON object and then hands
    # control back to CTranslate2, whose CUDA teardown returned
    # STATUS_STACK_BUFFER_OVERRUN on 3 runs out of 3 on this machine
    # (2026-08-28) after a transcription that was finished and correct.
    # Reading the exit code first threw that work away and reported an engine
    # that had "exited -1073740791 without saying why".
    #
    # This is not a weakened check. Every FAILURE path in the runner writes
    # `ok: false` before exiting, and a payload truncated by a crash mid-write
    # does not parse -- so both still land below. What changes is only the
    # case where the work demonstrably finished.
    if not payload.get("ok"):
        message = payload.get("error") or (
            f"the speech-recognition engine exited {process.returncode} "
            f"without saying why"
        )
        if process.returncode == EXIT_UNUSABLE:
            raise AsrUnavailable(message)
        raise MfpError(message)
    if process.returncode != 0:
        say(f"note: the engine finished its work and then exited "
            f"{process.returncode} on the way out. The transcript below is "
            f"the one it produced")

    # The caller judges this; a run that produced nothing is a legitimate
    # outcome here, because the ladder above is what decides whether to
    # spend another attempt on it.
    return payload


def _last_json_object(stdout: str) -> dict:
    """The result object, tolerant of anything printed before it.

    The contract says stdout carries one JSON object and nothing else, but
    a library the runner imports is free to print a warning there, and a
    warning must not be reported to the user as a broken engine. The LAST
    parseable line wins because the result is written last.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
