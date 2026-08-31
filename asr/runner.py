"""The speech-recognition half of `mfp transcript`, in someone else's venv.

This file is NOT part of the `mfp` package and must never import from it.
That is the whole point of it being here rather than under `src/mfp/`:
faster-whisper, CTranslate2 and their CUDA runtime are ~400 MB of Python
plus a ~3 GB model, and the product's installer is ~107 MB. Bundling them
would multiply the installer by twenty for a capability most runs never
use, so the engine lives in a separate interpreter and `mfp` talks to it
the same way it talks to yt-dlp and ffmpeg: as an external tool, over a
process boundary, with JSON in between.

The contract, which `src/mfp/asr.py` is the only reader of:

  stdout  ONE JSON object, at the end, and nothing else ever.
  stderr  Progress as `@asr <json>` lines, plus human text passed through.
  exit    0 on success. 2 for "the engine is not usable here" (missing
          package, missing model, no CUDA) so the caller can tell an
          environment problem from a file it could not read (1).

Two behaviours here are decisions rather than defaults:

**The audio is decoded ONCE.** `detect_language` needs an ndarray and
`transcribe` accepts one, so decoding twice would pay PyAV's cost twice for
no gain. It also means the format question is settled in exactly one place:
whatever PyAV/FFmpeg can open, this accepts.

**The language is MAPPED across the file, not decided once for it.**
faster-whisper builds its `Tokenizer` once, before the segment loop, and
every 30-second window is then decoded with that language token forced --
whether the caller named the language or detection guessed it. Detection
itself stops at the first window that clears its threshold, so on a
recording with a clear opening, window 1 decides the next fifty minutes.

Measured on a real 51-minute meeting (spike-07): 72 of 103 windows were
Mandarin, all 103 were decoded as English, and the result was fluent English
invention over Chinese speech, an 87-cue loop, and two passages that came
back as `Thank you very much.` repeated. So the file is scanned window by
window first, the votes are smoothed into contiguous STRETCHES, and each
stretch is decoded in its own language with its own instruction. The scan
costs 4% of the decode it precedes.

Whisper writes Simplified Chinese for `zh` unless told otherwise, and the
only lever that changes it is a prompt -- which is a hint, so it was measured
rather than assumed (2026-08-27: 简体 without it, 繁體 with it, on the same
clip). Feeding a Chinese instruction to English audio would bias the
transcription of a language that never needed it, which is why it is chosen
per stretch rather than per file.

**The script instruction is carried by `hotwords` ALONE, and never by
`initial_prompt`.** Whisper transcribes in 30-second windows, and each window
is prompted with `all_tokens[prompt_reset_since:][-223:]` -- read out of
faster-whisper 1.2.1's own `generate_segments` / `get_prompt`, where
`max_length` is 448. `initial_prompt` is prepended to `all_tokens` once, so
it scrolls out after roughly 223 tokens; `hotwords` is re-inserted by
`get_prompt` on every window and is not subject to `prompt_reset_since`.

`initial_prompt` is not merely weaker here, it is HARMFUL. On a Chinese
recording carrying English technical terms -- 晶晶體, the ordinary register of
Taiwanese engineering and research speech -- a Chinese `initial_prompt`
deleted **all 26** of them, leaving sentences with holes where the
load-bearing words had been. Two differently-worded prompts both scored
0/26, one of which explicitly asked for English to be preserved, so it is the
mechanism rather than the wording: `initial_prompt` joins `all_tokens`, which
the model reads as "the transcript so far", and a pure-Chinese seed says this
is a transcript with no English in it. `hotwords` sits outside that running
text and only biases, which is why it can steer the script without rewriting
what the speaker said.

What `hotwords` is worth for the SCRIPT was measured separately, and the
first prediction there was wrong too. On a clean five-minute Chinese
recording, `initial_prompt` alone held Traditional for all 69 lines --
because the model's own Traditional output keeps refilling the window and
sustains itself once started. Scrolling out, by itself, costs nothing.

The failure is the RESET. When a window needs a temperature above
`prompt_reset_on_temperature` (0.5), `prompt_reset_since` jumps to the end
of `all_tokens` and the next window runs with no previous text AND no
prompt. Forcing that condition on every window
(`condition_on_previous_text=False`) separates the arms, counting Simplified
characters in a recording that should have none:

    no instruction at all     180
    initial_prompt only       154
    hotwords                   23 – 32

Resets are not hypothetical: the same five-minute recording logged TEN of
them, and a talk show -- applause, theme music, people talking over each
other -- is made of the passages that cause them.

**Nothing is silently trusted about a long transcription.** Whisper's known
long-audio failure is degeneration -- the same line repeated until the audio
ends -- and it is invisible to every check that looks at whether a file was
produced. The per-segment `temperature`, `avg_logprob` and repetition are
counted here and reported in `health`, because a gate may only rule on what
it can observe and this is what can be observed. It found one on its first
real run: seven identical lines, with timestamps past the end of the audio.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from fractions import Fraction

#: The standing instruction handed to every 30-second window when the audio
#: is Chinese and the caller asked for Traditional. It does TWO jobs, and the
#: second one was measured into it after the first shipped:
#:
#:   1. Ask for Traditional. The sentence is written IN the script it is
#:      asking for -- that is the mechanism, not decoration.
#:   2. Say that English stays English. Taiwanese technical and academic
#:      speech is routinely 晶晶體 -- "先 review 這個 pull request" -- and
#:      without this clause Whisper drops or mangles the English terms, which
#:      are the load-bearing words in that kind of recording.
#:
#: Deliberately short. `get_prompt` allows hotwords up to `max_length // 2 - 1`
#: (223) tokens AND the previous text the same again, and the two together
#: must still leave room inside Whisper's 448-token context for the window's
#: own output. A short standing instruction costs a few tokens per window; a
#: long one eats the conversation it is supposed to keep coherent.
#:
#: An INSTRUCTION rather than an example, and that is a measured choice.
#: "我們先 review 這個 pull request，再決定要不要 merge。" scored best of all
#: on code-switched audio (24/26 terms) and then hallucinated twenty English
#: words into a pure-Chinese recording -- "nobody can tell if you are still"
#: appearing in a Mandarin monologue. A `hotwords` string biases toward the
#: tokens it contains, so it must not contain vocabulary the recording has no
#: reason to use.
ZH_HOTWORDS = "繁體中文，英文詞彙保留英文。"

#: How much of the context window the standing instruction may take before
#: it is doing more harm than good. Checked rather than assumed, because a
#: future edit to the sentence above is exactly the kind of change that
#: would not look like it broke anything.
MAX_HOTWORD_TOKENS = 40

#: A run of this many identical consecutive segments is Whisper's known
#: long-audio degeneration, not speech. Three, because two is a person
#: repeating themselves for emphasis and a talk show is full of that.
REPEAT_RUN_ALERT = 3

#: faster-whisper's own `prompt_reset_on_temperature` default. A window that
#: needed a temperature above this had its running context discarded, so
#: counting them counts the seams in the transcript's continuity. Mirrored
#: rather than imported because it is a default we do not override, and if a
#: future version changes it this constant is what will be wrong out loud.
PROMPT_RESET_TEMPERATURE = 0.5

#: faster-whisper's own `log_prob_threshold` default. Below it, the engine
#: itself considers the window a failed decode.
LOW_CONFIDENCE_LOGPROB = -1.0

#: Ceiling on how many 30-second windows of SPEECH the language vote may
#: consider. faster-whisper's default is 1, which on a talk show is the theme
#: tune. Detection stops at the first window that clears
#: `language_detection_threshold`, so this costs nothing on a clear-cut file
#: and only bites where the opening is ambiguous -- which is the case it is
#: here for. It cannot overrule a confident wrong answer, and does not claim
#: to (measured 2026-08-28).
LANGUAGE_DETECTION_SEGMENTS = 6

#: Below this, the detected language is reported as a guess rather than an
#: answer. It does not change what runs -- the user is the one who knows what
#: language the recording is in, and `--language` is what settles it.
LANGUAGE_UNSURE = 0.7

#: One language-map window, in seconds. Whisper's own window, because the
#: map is built from the same encoder output the decoder will use -- there is
#: no finer unit available and pretending otherwise would invent precision.
LANGUAGE_WINDOW_SECONDS = 30.0

#: What faster-whisper decodes audio to, and therefore what a second of it
#: costs in samples. Mirrored rather than imported so the pure half of this
#: file stays importable without the engine installed.
SAMPLE_RATE = 16000

#: Run-up handed to a stretch beyond its own bounds. A decoder given audio
#: that starts mid-sentence produces a worse first cue; the overlap is then
#: resolved by keeping each cue in the stretch its MIDPOINT falls in, so
#: neither neighbour emits it twice.
STRETCH_PAD_SECONDS = 5.0

#: How many consecutive windows must agree before a passage is a stretch of
#: its own. Swept on a real 51-minute bilingual recording (spike-07):
#:
#:     1 window  (0.5 min)   14 stretches -- fitted to single-window noise
#:     2 windows (1.0 min)   10
#:     4 windows (2.0 min)    4 -- the four passages a listener hears
#:     6 windows (3.0 min)    2 -- the bilingual middle collapses into zh
#:    10 windows (5.0 min)    2 -- the same middle collapses into en
#:
#: Four. Six and above destroy real structure, and that is the direction a
#: smoothing constant gets tuned in when nobody is watching the failure it
#: causes: over-smoothing produces a CLEANER-looking plan that is wrong.
MIN_STRETCH_WINDOWS = 4

#: How many windows a language must win before it is a candidate at all.
#: The detector offered `ms` twice and `ko` once on a recording containing
#: neither; restricting the vote to languages that actually carry the file
#: removed every one of those without a confidence threshold having to be
#: tuned (spike-07 4a: `ms=0.41` becomes `zh=0.54`, `ko=0.22` disappears).
MIN_CANDIDATE_WINDOWS = 4

#: Below this share of its own windows agreeing with it, a stretch is
#: REPORTED as contested rather than presented as settled. It changes
#: nothing about what runs. On the spike-07 recording the two settled
#: passages scored 1.00 and 0.97 and the two genuinely bilingual ones 0.60
#: and 0.64, so the gap is wide and this floor sits in it.
CONTESTED_AGREEMENT = 0.8

#: What `language` reads in the result when the file carried more than one.
#: ISO 639-2's code for "multiple languages" (user ruling 2026-08-31), so
#: the transcript's own name says what it is rather than claiming to be the
#: language that happened to occupy the most minutes.
MULTI_LANGUAGE = "mul"

#: A cue is the standing instruction rather than speech when it is short and
#: carries one of the instruction's own clauses. Matched loosely on purpose:
#: the capture measured on real audio was `中文詞彙保留英文。`, a CORRUPTED
#: form of `繁體中文，英文詞彙保留英文。` that an exact clause match misses
#: entirely (spike-07 6). A check defeated by the corruption of its target
#: is not a check.
INSTRUCTION_ECHO_MAX_CHARS = 24

#: Exit code meaning "this environment cannot run speech recognition".
#: Separate from 1 so `mfp` can say "install the engine" instead of
#: "that file could not be read", which are different user actions.
EXIT_UNUSABLE = 2


def note(message: str) -> None:
    """A line for the human. stderr, always -- stdout carries the result."""
    print(message, file=sys.stderr, flush=True)


def peak_memory_mb() -> float | None:
    """This process's high-water memory, or `None` where it cannot be asked.

    Reported rather than estimated. Whisper builds the log-mel spectrogram for
    the WHOLE file before transcribing a single window (faster-whisper 1.2.1,
    `transcribe`: `features = self.feature_extractor(audio, ...)`), so the
    peak is set by duration and not by anything the caller can tune -- and a
    two-hour recording is where that stops being an academic point. A real
    number from the user's own machine is worth more than any coefficient
    written down here, which would start rotting the moment the engine
    changed.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as wt

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD),
                    ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # `argtypes`/`restype` are NOT optional here, and the failure they
            # prevent is silent. Without them ctypes passes the pseudo-handle
            # as a 32-bit int, GetProcessMemoryInfo returns 0, and this
            # function answers "unknown" on the one platform it was written
            # for -- measured 2026-08-28, which is the only reason it is not
            # still doing that.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wt.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            psapi.GetProcessMemoryInfo.restype = wt.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wt.HANDLE, ctypes.POINTER(Counters), wt.DWORD
            ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return None
            return round(counters.PeakWorkingSetSize / 1024 / 1024, 1)

        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, macOS bytes.
        divisor = 1024 if sys.platform.startswith("linux") else 1024 * 1024
        return round(peak / divisor, 1)
    except Exception:  # noqa: BLE001 - a number this cannot get is not a failure
        return None


def progress(**fields: object) -> None:
    """A machine-readable progress record on the HUMAN's channel.

    On stderr because it is progress, and progress is not the result; the
    `@asr` marker is what lets `mfp` pick these out of whatever else
    faster-whisper or PyAV decides to print there.
    """
    print("@asr " + json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


def emit(payload: dict) -> None:
    """The result, on stdout, FLUSHED.

    The flush is not a formality. stdout is a pipe or a file here, so it is
    block-buffered, and this process can die on the way out through no fault
    of its own: CTranslate2's CUDA teardown returned STATUS_STACK_BUFFER_
    OVERRUN on 3 runs out of 3 on this machine (2026-08-28), AFTER a complete
    and correct transcription. Without the flush the buffer went with it and
    36 seconds of work reported as an engine that produced no output.
    """
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def fail(message: str, *, unusable: bool = False) -> int:
    """One shape for every failure: JSON on stdout, prose on stderr.

    The caller parses stdout unconditionally, so a failure that printed
    nothing there would surface as "the engine produced no output" -- true,
    unhelpful, and indistinguishable from a crash.
    """
    emit({"ok": False, "error": message})
    note(message)
    return EXIT_UNUSABLE if unusable else 1


class Health:
    """What the engine noticed about its own output, counted as it arrives.

    A class rather than four local counters so it can be TESTED without a
    3 GB model: `observe` takes anything with the four attributes Whisper's
    `Segment` carries. P-46 is the reason -- accounting that lives inside
    `main()` is accounting nothing can check until an end-to-end run, and by
    then it has already shipped.

    Nothing here changes the transcript. It changes what the caller is able
    to SAY about the transcript, which on a two-hour recording is the whole
    difference between handing over a file and handing over a file you know
    something about.
    """

    def __init__(self) -> None:
        self.kept = 0
        self.fallbacks = 0       # windows retried at a higher temperature
        self.context_drops = 0   # ...far enough to throw the running context away
        self.low_confidence = 0
        self.longest_repeat = 0
        self._repeat_run = 0
        self._previous: str | None = None
        self.repeat_text = ""

    def observe(self, segment: object, text: str) -> None:
        """One segment. `text` is the STRIPPED text, because a blank segment
        still says something about temperature even though it is dropped
        from the transcript."""
        temperature = float(getattr(segment, "temperature", 0.0) or 0.0)
        if temperature > 0:
            self.fallbacks += 1
        if temperature > PROMPT_RESET_TEMPERATURE:
            self.context_drops += 1
        if float(getattr(segment, "avg_logprob", 0.0) or 0.0) < LOW_CONFIDENCE_LOGPROB:
            self.low_confidence += 1
        if not text:
            return
        self.kept += 1
        self._repeat_run = self._repeat_run + 1 if text == self._previous else 1
        if self._repeat_run > self.longest_repeat:
            self.longest_repeat, self.repeat_text = self._repeat_run, text
        self._previous = text

    @property
    def degenerated(self) -> bool:
        return self.longest_repeat >= REPEAT_RUN_ALERT

    def result(self) -> dict:
        return {
            "segments": self.kept,
            "peakMemoryMB": peak_memory_mb(),
            "temperatureFallbacks": self.fallbacks,
            "contextDrops": self.context_drops,
            "lowConfidence": self.low_confidence,
            "longestRepeatRun": self.longest_repeat,
            "repeatedText": self.repeat_text if self.degenerated else None,
            "degenerated": self.degenerated,
        }

    def notes(self) -> list[str]:
        """Lines for the human, or none. NOT failures: a transcript with a
        stuck passage is still worth having, and refusing to hand it over
        would destroy the good 95% to protest the bad 5%."""
        lines = []
        if self.degenerated:
            lines.append(
                f"WARNING: {self.longest_repeat} identical lines in a row "
                f"({self.repeat_text[:40]!r}) -- that passage is very likely "
                f"the engine looping rather than speech"
            )
        if self.context_drops:
            lines.append(
                f"note: {self.context_drops} passage(s) were hard enough that "
                f"the engine dropped its running context there"
            )
        return lines


def evidence_of(segment: object, *, words: bool = False,
                offset: float = 0.0) -> dict:
    """What the decoder said about its own certainty at this segment.

    Every one of these numbers is computed by the decoder anyway. Until now
    `Health` read them, counted the ones past a threshold, and threw the
    values away -- so the product could say "eleven segments were shaky" and
    could not say WHICH. That is the whole difference between a report and
    evidence: a post-hoc correction stage must not move a word the engine was
    sure about, and `lowConfidence: 11` cannot tell it which word that is.

    Nothing here costs a second pass. `words` does: it asks faster-whisper
    for a per-word alignment, which is why it is opt-in.

    `getattr` throughout, because a field an older engine does not have must
    read as "not measured" rather than as a confident zero -- the same rule
    `duration_after_vad` follows below.
    """
    out: dict = {}
    for key, attr in (("avgLogprob", "avg_logprob"),
                      ("noSpeechProb", "no_speech_prob"),
                      ("compressionRatio", "compression_ratio"),
                      ("temperature", "temperature")):
        value = getattr(segment, attr, None)
        if value is not None:
            out[key] = round(float(value), 4)
    if not words:
        return out
    found = getattr(segment, "words", None) or []
    # `offset` because a stretch is decoded from a SLICE of the audio, so
    # every timing the engine reports is relative to that slice. The cue's
    # own start and end are corrected by the caller; if these were not, a
    # transcript would carry word timings that disagree with the cue
    # containing them -- and nothing downstream compares the two.
    out["words"] = [
        {"start": round(float(word.start) + offset, 3),
         "end": round(float(word.end) + offset, 3),
         "word": word.word,
         "prob": round(float(word.probability), 4)}
        for word in found
    ]
    return out


# --- the language plan -----------------------------------------------------
#
# Everything in this section is PURE: it takes the detector's per-window
# output and returns a plan. No model, no audio, no subprocess. That is
# deliberate and it is the same argument `mfp.quality` was built on -- the
# thing being guarded against is a smoothing rule that looks reasonable and
# quietly erases a real passage, and the only way to hold a rule like that
# still is to be able to test it exhaustively without a GPU.
#
# The problem it solves: faster-whisper decides the language ONCE, before
# the segment loop, and every 30-second window is then decoded with that
# language token forced -- whether the caller named it or detection guessed
# it. On a recording that changes language partway through, roughly 70% of
# a real 51-minute file was decoded in the wrong language, producing fluent
# English invention over Mandarin speech and, in two passages, nothing at
# all (spike-07).


def restricted_vote(
    top: list[tuple[str, float]], allowed: set[str] | frozenset[str]
) -> tuple[str | None, float]:
    """One window's verdict, restricted to the languages this file contains.

    Returns `(language, share)` where `share` is the winner's portion of the
    probability mass held by the allowed languages -- NOT its raw
    probability. The distinction is the whole value of this function: a
    window that reads `ms=0.41 zh=0.24 en=0.21` has no confident answer on
    the raw scale and a clear one on this one (`zh`, 0.54), because `ms` is
    not a language the recording is in.
    """
    hits = [(lang, prob) for lang, prob in top if lang in allowed]
    if not hits:
        return None, 0.0
    total = sum(prob for _, prob in hits)
    lang, prob = max(hits, key=lambda pair: pair[1])
    return lang, (prob / total if total else 0.0)


def candidate_languages(
    votes: list[list[tuple[str, float]]], *, declared: list[str] | None = None
) -> tuple[frozenset[str], dict[str, int]]:
    """Which languages this recording is actually in, and the raw tally.

    `declared` short-circuits it: a caller who says the recording is Mandarin
    and English is stating a fact about their own audio, and no amount of
    detector noise should out-vote them.

    Derived otherwise from a majority of windows rather than from any single
    confident window, because the failure being avoided is a one-window
    excursion becoming a stretch of its own. The tally is returned alongside
    so the result can be published with the evidence for it.
    """
    tally: dict[str, int] = {}
    for top in votes:
        if not top:
            continue
        tally[top[0][0]] = tally.get(top[0][0], 0) + 1
    if declared:
        return frozenset(declared), tally
    keep = {lang for lang, n in tally.items() if n >= MIN_CANDIDATE_WINDOWS}
    if not keep and tally:
        # Every language is below the floor: a very short file, or one the
        # detector has no opinion about. Take the plurality rather than
        # returning nothing, because an empty candidate set would leave every
        # window unvoted and the plan would be an artifact of the floor.
        keep = {max(tally, key=lambda lang: tally[lang])}
    return frozenset(keep), tally


def language_stretches(
    votes: list[list[tuple[str, float]]],
    allowed: frozenset[str] | set[str],
    *,
    window_seconds: float = LANGUAGE_WINDOW_SECONDS,
    min_windows: int = MIN_STRETCH_WINDOWS,
    total_seconds: float | None = None,
) -> list[dict]:
    """Contiguous passages, each with the language it will be decoded in.

    Runs shorter than `min_windows` are absorbed into the LONGER neighbour
    and the result re-merged, repeatedly, until nothing is left below the
    floor. Absorbing into the longer side rather than the earlier one is what
    stops a plan from being decided by which end of the file it was walked
    from.

    Each stretch reports `agreement`: the share of its own windows that voted
    for it. A passage where two languages alternate faster than the window
    cannot be resolved at this scale by anything -- so it is REPORTED at 0.6
    rather than presented at 1.0, and the reader is told which minutes to
    distrust. That number is the honest part of this function.
    """
    if not votes:
        return []
    settled: list[str | None] = [restricted_vote(top, allowed)[0] for top in votes]
    # A window with no allowed language at all inherits its predecessor --
    # silence and music are not a language change.
    fallback = next((lang for lang in settled if lang), sorted(allowed)[0] if allowed else "en")
    filled: list[str] = []
    for lang in settled:
        filled.append(lang or (filled[-1] if filled else fallback))

    runs: list[list] = []
    for index, lang in enumerate(filled):
        if runs and runs[-1][0] == lang:
            runs[-1][2] = index
        else:
            runs.append([lang, index, index])

    while len(runs) > 1:
        short = next(
            (i for i, (_, a, b) in enumerate(runs) if b - a + 1 < min_windows), None
        )
        if short is None:
            break
        lang, a, b = runs[short]
        left = runs[short - 1] if short > 0 else None
        right = runs[short + 1] if short + 1 < len(runs) else None
        if right is None:
            target = left
        elif left is None:
            target = right
        else:
            target = left if (left[2] - left[1]) >= (right[2] - right[1]) else right
        target[1], target[2] = min(target[1], a), max(target[2], b)
        runs.pop(short)
        merged: list[list] = []
        for lang2, a2, b2 in runs:
            if merged and merged[-1][0] == lang2:
                merged[-1][2] = max(merged[-1][2], b2)
            else:
                merged.append([lang2, a2, b2])
        runs = merged

    end_of_audio = (
        total_seconds if total_seconds is not None else len(votes) * window_seconds
    )
    plan = []
    for lang, a, b in runs:
        agree = sum(
            1 for top in votes[a:b + 1] if restricted_vote(top, allowed)[0] == lang
        )
        plan.append({
            "language": lang,
            "start": round(a * window_seconds, 3),
            "end": round(min((b + 1) * window_seconds, end_of_audio), 3),
            "windows": b - a + 1,
            "agreement": round(agree / (b - a + 1), 2),
        })
    return plan


def plan_language(plan: list[dict]) -> str | None:
    """What the whole transcript should be CALLED. `mul` when it is mixed."""
    languages = {stretch["language"] for stretch in plan}
    if not languages:
        return None
    return languages.pop() if len(languages) == 1 else MULTI_LANGUAGE


def instruction_clauses(instruction: str | None) -> list[str]:
    """The standing instruction, split into the pieces it comes back as."""
    if not instruction:
        return []
    parts = [part.strip() for part in re.split(r"[，,。．.、;；\s]+", instruction)]
    return [part for part in parts if len(part) >= 4]


def is_instruction_echo(text: str, instruction: str | None, *,
                        min_run: int = 6) -> bool:
    """Is this cue the engine writing out its own instruction?

    Matched by a shared RUN of characters rather than by a whole clause,
    because the capture measured on real audio was `中文詞彙保留英文。` --
    a corrupted form of `繁體中文，英文詞彙保留英文。` that contains neither
    clause as a substring and would sail past an exact match (spike-07 6).
    Six characters, and only on a cue short enough to be nothing but the
    instruction: a real sentence that happens to contain 「詞彙保留英文」 is
    longer than this, and 「保留英文」 on its own is under the run length.
    """
    if not instruction:
        return False
    body = text.strip().strip("。.,，、！!？? ")
    if len(body) < min_run or len(body) > INSTRUCTION_ECHO_MAX_CHARS:
        # SHORTER than the run is not a near-miss, it is a different thing:
        # 「英文」 is a word people say, and a cue that cannot contain a run
        # of `min_run` characters cannot be evidence of one.
        return False
    return any(
        body[i:i + min_run] in instruction
        for i in range(0, len(body) - min_run + 1)
    )


def echo_spans(cues: list[dict], instruction: str | None) -> list[tuple[float, float]]:
    """The `(start, end)` of every contiguous run of instruction cues.

    Runs rather than individual cues, because the repair re-decodes audio and
    a decoder handed two seconds of context produces worse output than one
    handed the whole stuck passage. On the recording this was measured
    against, one span covered 40:19-45:16 and carried 162 characters where
    the same audio without the instruction carries 823.
    """
    spans: list[list[float]] = []
    for cue in cues:
        if is_instruction_echo(str(cue.get("text", "")), instruction):
            if spans and cue["start"] - spans[-1][1] <= LANGUAGE_WINDOW_SECONDS:
                spans[-1][1] = float(cue["end"])
            else:
                spans.append([float(cue["start"]), float(cue["end"])])
    return [(a, b) for a, b in spans]


# --- the parts of the plan that need the model -----------------------------


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def _wants_instruction(language: str | None, script: str) -> bool:
    """Does THIS stretch get the standing Chinese instruction?

    Per stretch, never per file. An English passage in a bilingual recording
    must not be handed a Chinese instruction: D-107 measured that a
    `hotwords` string carrying the wrong vocabulary hallucinates it into the
    transcript, and a Chinese sentence is the wrong vocabulary for English
    audio by definition.
    """
    return script == "trad" and language == "zh"


def _instruction_budget(model, instruction: str) -> int | None:
    """How many tokens the standing instruction costs, per window."""
    try:
        return len(model.hf_tokenizer.encode(" " + instruction.strip()).ids)
    except Exception:  # noqa: BLE001 - a tokenizer this cannot ask is not fatal
        return None


def scan_languages(model, features) -> list[dict]:
    """PASS 1: what language is being spoken, window by window, all the way.

    One encoder pass and one decoder step per 30-second window. The encoder
    is the same work the decode will do anyway and the decoder step is a
    single forward, which is why this is affordable: measured at 20.3s over
    103 windows against 433.7s of decoding on the same file -- **4%**.

    Deliberately NOT `model.detect_language(audio=...)`. That helper returns
    as soon as one window clears its threshold, which is the correct
    behaviour for "what language is this file" and useless for "where does it
    change". Every window is classified here, including the ones the
    single-shot detector would never have reached.

    The top five are kept rather than the winner, because the winner alone
    cannot be restricted to a candidate set afterwards -- and restricting the
    vote is what removes the languages a recording is not in.
    """
    from faster_whisper.audio import pad_or_trim

    step = model.feature_extractor.nb_max_frames
    rows: list[dict] = []
    for index in range(0, features.shape[-1], step):
        encoded = model.encode(pad_or_trim(features[..., index:index + step]))
        results = model.model.detect_language(encoded)[0]
        rows.append({
            "at": round(len(rows) * LANGUAGE_WINDOW_SECONDS, 1),
            "top": [(token[2:-2], round(float(prob), 4))
                    for token, prob in results[:5]],
        })
        progress(phase="scan", at=rows[-1]["at"], windows=len(rows))
    return rows


def decode_stretch(
    model, audio, start: float, end: float, *,
    language: str, hotwords: str | None, args, total_seconds: float, counter: dict,
) -> tuple[list[tuple[object, str, float, float]], float | None]:
    """One stretch, decoded in its own language, timestamps put back.

    Decoded from a SLICE with `STRETCH_PAD_SECONDS` of run-up on each side,
    and cues are kept by the MIDPOINT rule -- a cue belongs to the stretch its
    middle falls in. The run-up exists because a decoder handed audio that
    begins mid-sentence produces a worse first cue; the midpoint rule exists
    because both neighbours would otherwise emit the overlap.

    Returns `(segment, text, absolute_start, absolute_end)` tuples rather than
    finished cue dicts, because the repair below has to be able to replace a
    span and `Health` has to be able to judge what survived it.
    """
    low = max(0.0, start - STRETCH_PAD_SECONDS)
    high = min(total_seconds, end + STRETCH_PAD_SECONDS)
    piece = audio[int(low * SAMPLE_RATE):int(high * SAMPLE_RATE)]
    segments, info = model.transcribe(
        piece,
        language=language,
        beam_size=args.beam_size,
        vad_filter=not args.no_vad,
        initial_prompt=None,
        # Re-applied to every window. See the module docstring: this is what
        # makes the instruction outlive the first two minutes.
        hotwords=hotwords,
        word_timestamps=args.words,
    )
    found: list[tuple[object, str, float, float]] = []
    for segment in segments:
        text = segment.text.strip()
        at, until = low + segment.start, low + segment.end
        if not (start <= (at + until) / 2 < end):
            continue
        found.append((segment, text, at, until))
        if text:
            counter["lines"] += 1
            progress(phase="segment", at=round(until, 2),
                     duration=round(total_seconds, 2), lines=counter["lines"])
    return found, getattr(info, "duration_after_vad", None)


def repair_instruction_capture(
    model, audio, found: list[tuple[object, str, float, float]], *,
    language: str, instruction: str, args, total_seconds: float, counter: dict,
) -> tuple[list[tuple[object, str, float, float]], list[dict]]:
    """Re-decode, WITHOUT the instruction, any span the instruction took over.

    The mechanism is specific and it is the flip side of the property D-102
    bought. `get_prompt` re-inserts `hotwords` on every window AND it is not
    subject to `prompt_reset_since`, so a window hard enough to force a
    temperature above `prompt_reset_on_temperature` runs with no previous
    text and the instruction as its ENTIRE prompt. The only thing left for
    the decoder to continue is the instruction, and it does.

    Measured on real multi-speaker audio (spike-07 6): 41:00-44:00 produced
    11 cues and 99 characters with the instruction, every one of them the
    instruction itself, against 85 cues and 512 characters without it. VAD
    kept 99% of that passage, so it was never a question of silence.

    Rejected: suppressing the instruction's token ids. The recording this was
    found on is ABOUT 中文 and 英文; those are legitimate words in it.
    """
    cues = [{"start": at, "end": until, "text": text}
            for _, text, at, until in found if text]
    spans = echo_spans(cues, instruction)
    if not spans:
        return found, []
    notes: list[dict] = []
    survivors = [entry for entry in found
                 if not any(low <= entry[2] < high for low, high in spans)]
    for low, high in spans:
        before = sum(len(text) for _, text, at, _ in found if low <= at < high)
        fresh, _ = decode_stretch(
            model, audio, low, high, language=language, hotwords=None,
            args=args, total_seconds=total_seconds, counter=counter,
        )
        after = sum(len(text) for _, text, _, _ in fresh)
        notes.append({"from": round(low, 2), "to": round(high, 2),
                      "charsBefore": before, "charsAfter": after})
        survivors += fresh
    survivors.sort(key=lambda entry: entry[2])
    return survivors, notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe one media file.")
    parser.add_argument("media", help="Any container FFmpeg can decode")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--model-dir", default=None,
                        help="Where model weights are cached. Nothing is "
                             "downloaded without --allow-download")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    parser.add_argument("--compute-type", default="auto",
                        help="auto picks float16 on CUDA, int8 on CPU. float16 "
                             "rather than int8 on CUDA is not a preference: "
                             "int8 hits CUBLAS_STATUS_NOT_SUPPORTED on "
                             "RTX 50-series (sm_120)")
    parser.add_argument("--language", default="auto",
                        help="ISO code, or auto to map the languages spoken "
                             "across the file. Naming one forces it on the "
                             "WHOLE recording, which is the right answer only "
                             "when the recording really is monolingual")
    parser.add_argument("--languages", default=None,
                        help="Comma-separated ISO codes the audio may contain, "
                             "e.g. zh,en. Detection then votes only among "
                             "these. Declaring them is worth doing: the "
                             "detector offers languages a recording is not in, "
                             "and one such window is enough to misplace a "
                             "passage. Ignored when --language names one")
    parser.add_argument("--script", default="trad", choices=["trad", "none"],
                        help="trad: ask for Traditional Chinese when the audio "
                             "is Chinese. Ignored for every other language")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--audio", default="none",
                        choices=["none", "level", "denoise"],
                        help="Pre-processing before recognition. none: the "
                             "recording unchanged, which is right for most "
                             "files. level: even out a lopsided recording. "
                             "denoise: for a loud speaker in a noisy room, "
                             "and measurably HARMFUL to a quiet one")
    parser.add_argument("--no-vad", action="store_true",
                        help="Disable voice-activity filtering. VAD is on by "
                             "default because silence is where Whisper "
                             "hallucinates")
    parser.add_argument("--allow-download", action="store_true",
                        help="Permit fetching model weights. Off by default: a "
                             "3 GB download nobody asked for looks like a hang")
    parser.add_argument("--words", action="store_true",
                        help="Also report per-word timings and probabilities. "
                             "Costs an extra alignment pass over every "
                             "segment, so it is off unless something "
                             "downstream needs to know WHERE the engine was "
                             "unsure rather than merely THAT it was")
    return parser


#: The pre-processing the caller may ask for, as FFmpeg filter chains.
#:
#: Both were MEASURED before either was offered, on a 5m26s two-person call
#: where one voice is far quieter than the other (2026-08-28). The number
#: that matters is how much audio survives voice-activity filtering, because
#: that is what reaches the decoder at all:
#:
#:     setting    VAD kept          transcript     retries
#:     unchanged   37.1s of 326.5s  (11%)    4 cues   4 of 4 windows
#:     level      319.7s            (98%)  137 cues   0
#:     denoise     37.0s            (11%)   16 cues   0
#:
#: `level` rescues a lopsided recording and it is not close: four cues become
#: 137. `denoise` does nothing for one -- a quiet speaker is exactly what a
#: denoiser removes, so it takes away as much as it recovers, and unlike the
#: unchanged case the ladder cannot save it either: turning the filter off
#: does not help when the speech is gone from the waveform. It is offered
#: anyway because a LOUD speaker in a noisy room is a real and different
#: case, and saying which is which is more honest than hiding the option.
#:
#: Measured THROUGH THIS FUNCTION, not through an `ffmpeg` command line. That
#: distinction earned its keep: the command line put `denoise` at 29.6s (9%)
#: and the graph here puts it at 37.0s, so quoting the command-line figure
#: would have described a filter the product does not run. `level` matches to
#: a tenth of a second, which is what says the two are the same filter.
#:
#: The wording a person reads lives in the GUI; this is the mechanism.
AUDIO_FILTERS = {
    "level": "dynaudnorm=f=150:g=15",
    "denoise": "highpass=f=80,afftdn=nf=-25,dynaudnorm=f=150:g=15",
}


def condition(audio, mode: str):
    """Run `audio` through one of `AUDIO_FILTERS`, in this process.

    Through PyAV's own filter graph rather than by shelling out to an
    `ffmpeg` binary. faster-whisper already depends on PyAV and PyAV carries
    its own FFmpeg libraries, so this adds no dependency, no PATH lookup and
    no second place for "which ffmpeg" to be answered differently. The
    engine venv is also not where this project's `binaries.ffmpeg` setting
    lives, and reaching across for it would put the runner in the business
    of reading the app's configuration.
    """
    import av
    import numpy as np

    chain = AUDIO_FILTERS[mode]
    graph = av.filter.Graph()
    source = graph.add_abuffer(
        format="flt", sample_rate=16000, layout="mono", time_base=Fraction(1, 16000)
    )
    tail = source
    for step in chain.split(","):
        name, _, arguments = step.partition("=")
        tail = _chain(graph, tail, name, arguments)
    sink = graph.add("abuffersink")
    tail.link_to(sink)
    graph.configure()

    frame = av.AudioFrame.from_ndarray(
        audio.reshape(1, -1), format="flt", layout="mono"
    )
    frame.sample_rate = 16000
    frame.time_base = Fraction(1, 16000)
    frame.pts = 0
    graph.push(frame)
    graph.push(None)

    chunks = []
    while True:
        try:
            out = graph.pull()
        except (av.error.BlockingIOError, av.error.EOFError):
            break
        chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError("the filter produced no audio")
    return np.concatenate(chunks).astype("float32")


def _chain(graph, tail, name: str, arguments: str):
    """One filter, linked onto the end of what is there so far."""
    node = graph.add(name, arguments) if arguments else graph.add(name)
    tail.link_to(node)
    return node


def resolve_device(requested: str) -> tuple[str, str]:
    """`(device, why)`. Falls back to CPU rather than failing.

    A machine with no GPU must still be able to transcribe -- slowly is a
    different outcome from not at all.
    """
    if requested == "cpu":
        return "cpu", "asked for"
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001 - any failure means "no CUDA"
        if requested == "cuda":
            raise RuntimeError(f"CUDA was asked for and is not usable: {exc}") from exc
        return "cpu", f"no usable CUDA ({exc})"
    if count > 0:
        return "cuda", f"{count} CUDA device(s)"
    if requested == "cuda":
        raise RuntimeError("CUDA was asked for and no CUDA device was found")
    return "cpu", "no CUDA device found"


def resolve_compute_type(requested: str, device: str) -> str:
    if requested != "auto":
        return requested
    return "float16" if device == "cuda" else "int8"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
    except ImportError as exc:
        return fail(
            f"this interpreter cannot import faster-whisper ({exc}). "
            f"Install it with:  {sys.executable} -m pip install faster-whisper",
            unusable=True,
        )

    try:
        device, why = resolve_device(args.device)
    except RuntimeError as exc:
        return fail(str(exc), unusable=True)
    compute_type = resolve_compute_type(args.compute_type, device)
    note(f"device: {device} ({why}), compute type: {compute_type}")

    # Decode first. It is the cheapest step that can fail, and failing here
    # means "that file", not "this machine" -- worth separating before the
    # model spends five seconds loading.
    try:
        t0 = time.perf_counter()
        audio = decode_audio(args.media)
    except Exception as exc:  # noqa: BLE001 - PyAV raises many shapes
        return fail(
            f"could not decode audio from {args.media}: {exc}. "
            f"If it is DRM-protected, no tool here can open it"
        )
    seconds = len(audio) / 16000.0
    note(f"decoded {seconds:.1f}s of audio in {time.perf_counter() - t0:.1f}s")
    if seconds <= 0:
        return fail(f"{args.media} carries no audio track at all")
    progress(phase="decoded", duration=round(seconds, 2))

    if args.audio != "none":
        try:
            t0 = time.perf_counter()
            audio = condition(audio, args.audio)
            note(f"audio: {AUDIO_FILTERS[args.audio]} applied in "
                 f"{time.perf_counter() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001 - a filter graph raises many shapes
            # Reported and skipped, never fatal. A pre-processing step the
            # user asked for that cannot run is a reason to transcribe the
            # original, not a reason to hand back nothing.
            note(f"note: could not apply the {args.audio!r} audio setting "
                 f"({exc}); transcribing the recording unchanged")
            args.audio = "none"

    try:
        t0 = time.perf_counter()
        model = WhisperModel(
            args.model,
            device=device,
            compute_type=compute_type,
            download_root=args.model_dir,
            local_files_only=not args.allow_download,
        )
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"could not load the {args.model!r} model: {exc}",
            unusable=True,
        )
    note(f"model {args.model} loaded in {time.perf_counter() - t0:.1f}s")
    progress(phase="loaded")

    forced = None if args.language == "auto" else args.language
    declared = (
        [code.strip() for code in args.languages.split(",") if code.strip()]
        if args.languages else None
    )

    language_map: list[dict] = []
    tally: dict[str, int] = {}
    detect_probability = None
    if forced is not None:
        # Named by the caller: one stretch covering the file, which is
        # exactly what every build before this did. A recording somebody has
        # listened to beats any detector, and the cost of being wrong is the
        # user's to accept.
        plan = [{"language": forced, "start": 0.0, "end": round(seconds, 3),
                 "windows": 0, "agreement": 1.0}]
        note(f"language: {forced} (named, not detected)")
    else:
        # PASS 1. Not one detection but a MAP, because faster-whisper decides
        # the language once and then forces that token on every window for
        # the rest of the file. On a 51-minute recording that changes
        # language halfway, that meant 72 of 103 windows were Mandarin and
        # all 103 were decoded as English (spike-07).
        #
        # `detect_language(audio=...)` cannot be used for this: it STOPS at
        # the first window over its threshold, which on a file with a clear
        # opening is window 1. So the windows are walked here.
        t0 = time.perf_counter()
        try:
            features = model.feature_extractor(audio)
            language_map = scan_languages(model, features)
        except Exception as exc:  # noqa: BLE001 - a scan that fails is not fatal
            # Fall back to the single-shot detector rather than refusing. A
            # worse plan is better than no transcript, and the result says
            # which one it got.
            note(f"note: could not map the languages ({exc}); "
                 f"falling back to detecting one for the whole file")
            forced, detect_probability, _ = model.detect_language(
                audio=audio, vad_filter=True,
                language_detection_segments=LANGUAGE_DETECTION_SEGMENTS,
            )
            plan = [{"language": forced, "start": 0.0, "end": round(seconds, 3),
                     "windows": 0, "agreement": 1.0}]
        else:
            votes = [window["top"] for window in language_map]
            allowed, tally = candidate_languages(votes, declared=declared)
            plan = language_stretches(votes, allowed, total_seconds=seconds)
            # This line, in these words, is what catches an audio track that
            # is not what was expected -- a video fetched as an English talk
            # that transcribes as `ar` is an AI-dubbed track, not a
            # recognition failure, and this is the only place a human sees
            # it. It names the flags that settle it, so it is an instruction
            # rather than a complaint.
            note(f"language detected: {', '.join(sorted(allowed))} "
                 f"-- mapped over {len(votes)} windows in "
                 f"{time.perf_counter() - t0:.1f}s, heard {tally}. If that is "
                 f"not what you expected, the audio may not be what you "
                 f"expected either; --language <code> forces one for the whole "
                 f"file, --languages limits what may be detected")
            for stretch in plan:
                note(f"  {_clock(stretch['start'])}-{_clock(stretch['end'])}  "
                     f"{stretch['language']}  "
                     f"({stretch['agreement']:.0%} of its windows agree)")
            contested = [s for s in plan if s["agreement"] < CONTESTED_AGREEMENT]
            if contested:
                # Reported, never smoothed away. A passage where the speakers
                # swap language faster than a 30-second window cannot be
                # resolved at this scale by anything, and saying which minutes
                # those are is worth more than a plan that looks tidy.
                note(f"note: {len(contested)} passage(s) had two languages "
                     f"arguing inside them; those minutes are less reliable "
                     f"than the rest")

    progress(phase="language", language=plan_language(plan), plan=plan)

    # NO `initial_prompt`, anywhere. It used to carry the script instruction
    # and it DELETES ENGLISH: on a code-switched Chinese recording, 0 of 26
    # technical terms survived -- every one silently dropped, leaving Chinese
    # sentences with holes where the load-bearing words had been. Two
    # differently-worded prompts scored 0/26, including one that explicitly
    # asked for English to be kept, so it is the mechanism and not the
    # wording: `initial_prompt` joins `all_tokens`, which the model reads as
    # "the transcript so far", and a pure-Chinese seed says this is a
    # transcript with no English in it.
    #
    # The instruction is PER STRETCH rather than per file. That is what keeps
    # D-102/D-106/D-107 exactly as they were measured -- a Chinese
    # instruction reaching an English passage is a condition none of them
    # tested, and D-107 already showed that a `hotwords` string containing
    # the wrong vocabulary hallucinates it into the transcript.
    if any(_wants_instruction(s["language"], args.script) for s in plan):
        budget = _instruction_budget(model, ZH_HOTWORDS)
        if budget is not None:
            note(f"script instruction: {budget} tokens per window")
            if budget > MAX_HOTWORD_TOKENS:
                return fail(
                    f"the standing Chinese instruction is {budget} tokens, "
                    f"over the {MAX_HOTWORD_TOKENS} this reserves per window. "
                    f"Shorten ZH_HOTWORDS -- an instruction this long crowds "
                    f"out the transcript it is steering"
                )

    # PASS 2. One decode per stretch, each with its own language and its own
    # instruction, and the running context resetting at every boundary --
    # which is a second benefit rather than a side effect: degeneration in
    # one passage used to be inherited by the next through
    # `condition_on_previous_text`.
    t0 = time.perf_counter()
    counter = {"lines": 0}
    kept_seconds = 0.0
    measured_vad = False
    repairs: list[dict] = []
    collected: list[tuple[object, str, float, float]] = []
    for stretch in plan:
        hotwords = (
            ZH_HOTWORDS if _wants_instruction(stretch["language"], args.script)
            else None
        )
        found, vad = decode_stretch(
            model, audio, stretch["start"], stretch["end"],
            language=stretch["language"], hotwords=hotwords,
            args=args, total_seconds=seconds, counter=counter,
        )
        if hotwords is not None:
            found, notes = repair_instruction_capture(
                model, audio, found,
                language=stretch["language"], instruction=hotwords,
                args=args, total_seconds=seconds, counter=counter,
            )
            repairs += notes
        collected += found
        if vad is not None:
            kept_seconds += float(vad)
            measured_vad = True
    collected.sort(key=lambda found: found[2])
    wall = time.perf_counter() - t0

    # Health is observed over what SURVIVED, not over what the decoder
    # emitted: a passage that was repaired must be judged on the words that
    # ship, or the product reports a stuck run it has already removed.
    monitor = Health()
    lines = []
    for segment, text, start, end in collected:
        monitor.observe(segment, text)
        if not text:
            continue
        line = {"start": round(start, 3), "end": round(end, 3), "text": text}
        line.update(evidence_of(segment, words=args.words,
                                offset=start - float(segment.start)))
        lines.append(line)

    health = monitor.result()
    # What the VAD kept, which is the one number that can see the failure
    # mode nothing else could: on a recording where one speaker is far
    # quieter than the other, silero classifies that speaker as non-speech
    # and 89% of the audio never reaches the decoder at all (UAT 2026-08-28,
    # measured 37.1s kept of 326.5s). The resulting transcript is short,
    # fluent and wrong -- and every instrument downstream looks at the TEXT,
    # where the deleted words leave no trace.
    #
    # `getattr` because an older faster-whisper has no such field, and a
    # missing number must read as "not measured" rather than as zero. Summed
    # across the stretches: each decode reports what ITS slice kept, and the
    # number this is compared against is the whole file.
    if measured_vad:
        health["vadSeconds"] = round(kept_seconds, 2)
        health["audioSeconds"] = round(seconds, 2)
    if repairs:
        # Counted in `health` rather than only noted, because the repair is
        # the product removing its own instruction from its own output --
        # exactly the kind of intervention that must leave a record.
        health["instructionRepairs"] = repairs
        recovered = sum(r["charsAfter"] - r["charsBefore"] for r in repairs)
        note(f"repaired {len(repairs)} passage(s) where the script "
             f"instruction had replaced the speech ({recovered:+d} characters)")
    for line in monitor.notes():
        note(line)
    # ...and on the machine's channel as well, because `say` is wired to a
    # terminal and NOT to the GUI: the server forwards `on_progress` onto the
    # event stream and drops the human lines. A warning only the CLI can see
    # is a warning the people most likely to hit this never get.
    progress(phase="health", **health)

    instruction = (
        ZH_HOTWORDS
        if any(_wants_instruction(s["language"], args.script) for s in plan)
        else None
    )
    emit({
        "ok": True,
        # `mul` when the file carried more than one language, so the
        # transcript's own name says what it is instead of claiming to be
        # whichever language happened to occupy the most minutes.
        "language": plan_language(plan),
        "languageProbability": round(
            detect_probability if detect_probability is not None
            else (sum(s["agreement"] for s in plan) / len(plan) if plan else 0.0),
            3,
        ),
        # The plan, and the raw per-window votes it was smoothed from. Both,
        # because a verdict published without the evidence for it is a number
        # nobody can check -- and because `mfp.quality` rules on the plan
        # against the TEXT, which it can only do if it has the plan.
        "languagePlan": plan,
        "languageMap": language_map,
        "languagesHeard": tally,
        "duration": round(seconds, 3),
        "segments": lines,
        "health": health,
        "engine": {
            "name": "faster-whisper",
            "model": args.model,
            "device": device,
            "computeType": compute_type,
            "script": args.script,
            # Kept as a field, and now always false: `initial_prompt` deletes
            # English from code-switched speech. A reader of an old result
            # can still tell which way it was produced.
            "promptUsed": False,
            "hotwordsUsed": instruction is not None,
            # The instruction VERBATIM, not a flag. A standing instruction
            # can be emitted as transcript content -- 「英文詞彙保留英文。」
            # appeared three times in a row in a delivered `.srt` (UAT
            # 2026-08-28) -- and the only way to check for that downstream is
            # to know what was actually said to the engine. Reported rather
            # than duplicated in `mfp.quality`, because a copy of this string
            # over there would go stale the first time this one is edited.
            "instruction": instruction,
            # What was actually done to the audio, which is not always what
            # was asked for: a filter that will not build is skipped with a
            # note rather than failing the run.
            "audio": args.audio,
            "wallSeconds": round(wall, 2),
            "realtimeFactor": round(seconds / wall, 1) if wall > 0 else None,
        },
    })
    note(f"{len(lines)} segments in {wall:.1f}s ({seconds / wall:.1f}x realtime)"
         if wall > 0 else f"{len(lines)} segments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
