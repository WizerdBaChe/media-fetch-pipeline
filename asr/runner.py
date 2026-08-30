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

**Language is detected BEFORE transcribing, so the prompt can be chosen.**
Whisper writes Simplified Chinese for `zh` unless told otherwise, and the
only lever that changes it is a prompt -- which is a hint, so it was measured
rather than assumed (2026-08-27: 简体 without it, 繁體 with it, on the same
clip). Feeding a Chinese prompt to English audio would bias the transcription
of a language that never needed it, which is why the prompt waits for the
detection instead of being passed unconditionally.

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


def evidence_of(segment: object, *, words: bool = False) -> dict:
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
    out["words"] = [
        {"start": round(float(word.start), 3),
         "end": round(float(word.end), 3),
         "word": word.word,
         "prob": round(float(word.probability), 4)}
        for word in found
    ]
    return out


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
                        help="ISO code, or auto to detect from the audio")
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

    language = None if args.language == "auto" else args.language
    detect_probability = None
    if language is None:
        # NOT the defaults. `detect_language(audio=audio)` classifies
        # `audio[:1 * n_samples]` -- the first thirty seconds, VAD off -- and
        # a talk show opens with a theme tune. `vad_filter=True` drops the
        # music before anything is classified, and LANGUAGE_DETECTION_SEGMENTS
        # raises the ceiling on how much speech may be considered.
        #
        # What this does NOT do is out-vote a confident first window:
        # detection returns as soon as one window passes
        # `language_detection_threshold`, so the ceiling only bites where the
        # early windows are unsure. Measured 2026-08-28 on a file that comes
        # back `ar` at 100% -- the default, VAD, six windows and a threshold
        # above 1.0 all returned the same answer, because every window agreed.
        # (That file turned out to BE Arabic: an AI-dubbed YouTube track. The
        # detector was right and the audio was not what was expected, which is
        # the whole argument for printing this line where a human sees it.)
        language, detect_probability, _ = model.detect_language(
            audio=audio,
            vad_filter=True,
            language_detection_segments=LANGUAGE_DETECTION_SEGMENTS,
        )
        note(f"language detected: {language} ({detect_probability:.0%}) -- "
             f"if that is not the language you expected, the audio may not be "
             f"what you expected either; --language <code> forces it")
        if detect_probability is not None and detect_probability < LANGUAGE_UNSURE:
            # Reported, not refused. A wrong guess here mislabels the whole
            # file, and the user is the one who knows what language it is --
            # so the answer is to say so, and to name the flag that settles it.
            note("note: that is not a confident answer. Naming the language "
                 "with --language <code> is better than letting it be guessed")
    progress(phase="language", language=language)

    wants_traditional = args.script == "trad" and language == "zh"
    # NO `initial_prompt`. It used to carry this instruction and it DELETES
    # ENGLISH: on a code-switched Chinese recording, 0 of 26 technical terms
    # survived -- every one silently dropped, leaving Chinese sentences with
    # holes where the load-bearing words had been. Two differently-worded
    # prompts scored 0/26, including one that explicitly asked for English to
    # be kept, so it is the mechanism and not the wording.
    #
    # The mechanism: `initial_prompt` is prepended to `all_tokens`, which
    # Whisper reads as "the transcript so far". A pure-Chinese seed tells the
    # model this is a transcript with no English in it, and it obliges.
    # `hotwords` sits outside that running text and merely biases, which is
    # why it steers the script without rewriting what the speaker said.
    #
    # Measured 2026-08-28 on the same audio: hotwords alone keeps 24 of 26
    # terms with zero Simplified characters, and 26 of 26 under the
    # post-reset condition D-102 is about.
    prompt = None
    hotwords = ZH_HOTWORDS if wants_traditional else None
    if hotwords is not None:
        # The one thing that would silently undo the whole mechanism is the
        # standing instruction growing until it crowds out the conversation.
        # Counted with the model's own tokenizer rather than by characters.
        try:
            budget = len(model.hf_tokenizer.encode(" " + hotwords.strip()).ids)
        except Exception:  # noqa: BLE001 - a tokenizer this cannot ask is not fatal
            budget = None
        if budget is not None:
            note(f"script instruction: {budget} tokens per window")
            if budget > MAX_HOTWORD_TOKENS:
                return fail(
                    f"the standing Chinese instruction is {budget} tokens, "
                    f"over the {MAX_HOTWORD_TOKENS} this reserves per window. "
                    f"Shorten ZH_HOTWORDS -- an instruction this long crowds "
                    f"out the transcript it is steering"
                )

    t0 = time.perf_counter()
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=args.beam_size,
        vad_filter=not args.no_vad,
        initial_prompt=prompt,
        # Re-applied to every window. See the module docstring: this is what
        # makes the instruction outlive the first two minutes.
        hotwords=hotwords,
        word_timestamps=args.words,
    )

    # `segments` is a generator: the work happens HERE, as it is consumed,
    # which is what makes a progress line possible at all.
    lines = []
    monitor = Health()
    for segment in segments:
        text = segment.text.strip()
        monitor.observe(segment, text)
        if not text:
            continue
        line = {"start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": text}
        line.update(evidence_of(segment, words=args.words))
        lines.append(line)
        progress(phase="segment", at=round(segment.end, 2),
                 duration=round(seconds, 2), lines=len(lines))
    wall = time.perf_counter() - t0

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
    # missing number must read as "not measured" rather than as zero.
    kept = getattr(info, "duration_after_vad", None)
    if kept is not None:
        health["vadSeconds"] = round(float(kept), 2)
        health["audioSeconds"] = round(seconds, 2)
    for line in monitor.notes():
        note(line)
    # ...and on the machine's channel as well, because `say` is wired to a
    # terminal and NOT to the GUI: the server forwards `on_progress` onto the
    # event stream and drops the human lines. A warning only the CLI can see
    # is a warning the people most likely to hit this never get.
    progress(phase="health", **health)

    emit({
        "ok": True,
        "language": info.language,
        "languageProbability": round(detect_probability or info.language_probability, 3),
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
            "promptUsed": prompt is not None,
            "hotwordsUsed": hotwords is not None,
            # The instruction VERBATIM, not a flag. A standing instruction
            # can be emitted as transcript content -- 「英文詞彙保留英文。」
            # appeared three times in a row in a delivered `.srt` (UAT
            # 2026-08-28) -- and the only way to check for that downstream is
            # to know what was actually said to the engine. Reported rather
            # than duplicated in `mfp.quality`, because a copy of this string
            # over there would go stale the first time this one is edited.
            "instruction": hotwords,
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
