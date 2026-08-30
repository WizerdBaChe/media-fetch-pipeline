"""Turn a transcript that already exists into another language.

Three rulings shape this module, and all three are the user's (2026-08-28).

**It is never part of transcribing.** `mfp transcript` produces a transcript
and stops. Translation is a separate verb over a caption file that already
exists, because the two are different decisions made at different moments --
and because folding them together would make every transcription pay for a
translation model it did not ask for.

**There has to be a source first.** The input is a `.srt`, `.vtt` or `.txt`,
never a media file. Anyone wanting both runs `mfp transcript` and then this;
that is one extra command and it keeps the recognised text on disk where it
can be read and corrected before anything is built on top of it.

**Nothing is translated without being asked.** No default target that quietly
applies, no automatic pass at the end of a recognition run.

The timings survive. A translated `.srt` keeps the ORIGINAL cue's start and
end, because the words changed and the moment they were said did not -- which
is also what lets `引用長圖` quote a translated transcript through `--subs`
with no special case, exactly as it quotes a recognised one.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mfp.asr import EXIT_UNUSABLE, AsrUnavailable, _last_json_object, to_srt
from mfp.errors import MfpError, UsageError
from mfp.cues import TAG, TIMESTAMP, seconds_of

__all__ = [
    "FLORES_BY_ISO",
    "TranslationOutcome",
    "TranslationUnavailable",
    "RECORD_INFIX",
    "flores_for",
    "parse_cues",
    "read_cues",
    "record",
    "record_name",
    "write_record",
    "read_source_text",
    "refuse_missing_source",
    "refuse_self_overwrite",
    "clauses_of",
    "looks_truncated",
    "run_translator",
    "runner_path",
    "translate_texts",
    "translate_file",
]


class TranslationUnavailable(AsrUnavailable):
    """No translation model on this machine -- a setup step, not a failure.

    Subclasses `AsrUnavailable` so it inherits `asr_unavailable` and exit 6,
    which is deliberate: from a caller's side "the recognition engine is not
    set up" and "the translation model is not set up" are the same sentence
    with a different noun, and an agent that knows what to do about one knows
    what to do about the other. The MESSAGE is what distinguishes them, and
    the message names which of the two it is.
    """


@dataclass(frozen=True)
class TranslationOutcome:
    #: The caption file written. Everything downstream reads THIS.
    source: Path
    target_language: str
    source_language: str
    line_count: int
    engine: dict
    #: Indices of lines the engine appears to have cut short (see
    #: `looks_truncated`). Carried on the outcome rather than only printed,
    #: because the GUI has no stderr to read and the person reading a
    #: translation is the one who needs to know it may be incomplete.
    suspect_lines: tuple[int, ...] = ()
    #: How many extra pieces `clauses_of` cut the input into. Zero means the
    #: engine saw exactly what the caller counted.
    clause_splits: int = 0
    #: The record written beside the translation, or None when the caller
    #: asked for the translation alone. Never a separate `place_new` call:
    #: the two names go through one `place_set` so they cannot land on
    #: different serials (P-62).
    record: Path | None = None

    def counts(self) -> dict[str, int]:
        """The summary numbers `record()` carries.

        A method rather than a dict built at the call site, because
        `DocumentOutcome` counts blocks as well as lines and the record
        writer must not have to know which outcome it was handed.
        """
        return {
            "lines": self.line_count,
            "clauseSplits": self.clause_splits,
            "suspectLines": len(self.suspect_lines),
        }


#: ISO-639-1 (what Whisper reports) -> FLORES-200 (what NLLB wants).
#:
#: Deliberately partial. A code that is not here raises and asks the user to
#: name the FLORES code, which is the honest outcome: FLORES distinguishes
#: things ISO-639-1 does not -- `zh` is `zho_Hans` OR `zho_Hant`, and picking
#: one silently is picking which script somebody's transcript is in.
#:
#: `zh` maps to Traditional because that is what this product asks Whisper
#: for (`AsrConfig.script`) and therefore what its own recognised transcripts
#: are in. A transcript from anywhere else may not be, which is why `--from`
#: exists.
FLORES_BY_ISO: dict[str, str] = {
    "en": "eng_Latn",
    "zh": "zho_Hant",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "es": "spa_Latn",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "it": "ita_Latn",
    "pt": "por_Latn",
    "ru": "rus_Cyrl",
    "ar": "arb_Arab",
    "hi": "hin_Deva",
    "th": "tha_Thai",
    "vi": "vie_Latn",
    "id": "ind_Latn",
    "ms": "zsm_Latn",
    "tr": "tur_Latn",
    "nl": "nld_Latn",
    "pl": "pol_Latn",
    "uk": "ukr_Cyrl",
}

#: Suffixes this verb reads. A media file is refused by name rather than
#: attempted, because "run `mfp transcript` first" is a better answer than
#: a decoder error.
CAPTION_SUFFIXES = frozenset({".srt", ".vtt", ".txt"})


def flores_for(code: str | None) -> str | None:
    """A FLORES-200 code from whatever the caller has, or `None`.

    Already-FLORES codes pass through untouched, so a user who typed
    `zho_Hant` is not made to look up an ISO code for it.
    """
    if not code:
        return None
    cleaned = code.strip()
    if re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", cleaned):
        return cleaned
    return FLORES_BY_ISO.get(cleaned.split("-")[0].lower())


#: Where a sentence may be cut so the engine does not lose half of it.
#:
#: Measured 2026-08-30 against `nllb-200-distilled-1.3B-ct2-int8`, which is
#: the model this product installs. `Previously it chose by bitrate alone,
#: which was a tie on YouTube.` came back as `之前它只選取比特速率,` -- the
#: second clause simply gone -- while the SAME two clauses sent separately
#: both translated. Beam size, `max_decoding_length`, `length_penalty` and
#: `min_decoding_length` were each tried and changed nothing; the split is
#: what recovers the words.
#:
#: Comma, semicolon and colon in both widths, plus the CJK enumeration
#: comma. The delimiter stays with the clause BEFORE it, so each piece is
#: still punctuated when it reaches the model and the join needs no
#: separator of its own.
_CLAUSE_END = re.compile(r"(?<=[,;:，；：、])\s*")

#: Scripts written without spaces between words. Joining translated clauses
#: with a space would put one inside a Chinese sentence.
#:
#: Korean is deliberately ABSENT: it is written in Hangul but it does space
#: its words (띄어쓰기), so `kor_Hang` joins like a Latin script. Grouping it
#: with the CJK neighbours it is usually listed beside would run its clauses
#: together.
_UNSPACED_SCRIPTS = ("Hans", "Hant", "Jpan", "Thai", "Laoo", "Mymr", "Khmr")

#: Below this many characters a clause is not worth cutting out: the engine
#: loses whole clauses, not fragments, and a two-character piece translated
#: alone has no context left to translate against.
_MIN_CLAUSE_CHARS = 12


def clauses_of(text: str) -> list[str]:
    """One sentence -> the clauses to send separately, or `[text]`.

    Returns the input unchanged when there is nothing worth splitting, so a
    caption cue -- which is usually one short clause already -- takes exactly
    the path it always did.

    Joining the pieces back is concatenation: every delimiter stays attached
    to the clause it closed, so `"".join(clauses_of(t)) == t` up to the
    whitespace that followed a delimiter.
    """
    stripped = text.strip()
    if len(stripped) <= _MIN_CLAUSE_CHARS:
        return [stripped] if stripped else []
    pieces = [p.strip() for p in _CLAUSE_END.split(stripped)]
    pieces = [p for p in pieces if p]
    if len(pieces) < 2:
        return [stripped]
    # A trailing scrap (`..., too.`) has no context of its own and is worth
    # less split off than left attached to what it qualifies.
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) < _MIN_CLAUSE_CHARS:
            merged[-1] = _join_clauses(merged[-1], piece, spaced=True)
        else:
            merged.append(piece)
    return merged if len(merged) > 1 else [stripped]


def _join_clauses(left: str, right: str, *, spaced: bool) -> str:
    if not left:
        return right
    if not right:
        return left
    if not spaced or left[-1].isspace() or right[0].isspace():
        return left + right
    return f"{left} {right}"


def joins_without_spaces(flores: str) -> bool:
    """Does this target language write without spaces between words?"""
    return flores.split("_")[-1] in _UNSPACED_SCRIPTS


def looks_truncated(source: str, translated: str) -> bool:
    """Did the engine stop in the middle of a clause?

    One deterministic fingerprint, deliberately, rather than a length ratio
    with a threshold nobody can calibrate: a source that ends a sentence and
    a translation that ends a CLAUSE is the shape the measured failures
    have. It catches some omissions and not all -- a dropped middle clause
    ends the output correctly -- so it is a warning, never a refusal, and
    the caller must not read a quiet run as a clean one.
    """
    source, translated = source.strip(), translated.strip()
    if not source or not translated:
        return False
    return source[-1] in ".。!！?？…" and translated[-1] in ",，;；:：、"


#: `<stem>.<flores>.translation.json`. The infix is a word rather than an
#: extension so a folder holding `talk.zh.corrections.json`,
#: `talk.zh.tidy.json` and `talk.eng_Latn.translation.json` says which
#: analysis wrote which without anybody opening them.
RECORD_INFIX = "translation"
SCHEMA_VERSION = 1


def record_name(stem: str, target_flores: str) -> str:
    """The record's file name, in one place because two writers use it."""
    return f"{stem}.{target_flores}.{RECORD_INFIX}.json"


def record(outcome: TranslationOutcome, *, source: Path) -> dict:
    """What a translation was made from, and by what.

    `correct` and `tidy` have each written a record beside their output
    since they shipped; the two translate verbs wrote only the product. So a
    `talk.eng_Latn.srt` on disk could not say what it was translated FROM,
    with which model, how many clauses had to be split to get it, or which
    of its lines the truncation check flagged — all of which existed in
    memory and reached stdout exactly once (F4 audit gap G1, 2026-08-30).

    Shaped like `tidy.record`: `schemaVersion`, `source`, `created`, a
    `summary` of counts, then what is specific to this verb.

    **`suspectLines` is 1-BASED here**, unlike `tidy`'s 0-based `cue`. Not an
    inconsistency to tidy away: every surface that has ever printed a
    `suspectLines` — the CLI's `--json`, `/v1/translate`, the GUI caution —
    counts from one, and one KEY meaning two things in two files is the
    disagreement P-59 is about.
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source.name,
        # The file this record describes. `place_set` already guarantees they
        # share a serial; naming it makes that checkable by reading rather
        # than by trusting.
        "written": outcome.source.name,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceLanguage": outcome.source_language,
        "targetLanguage": outcome.target_language,
        "engine": outcome.engine,
        "summary": outcome.counts(),
        "suspectLines": [index + 1 for index in outcome.suspect_lines],
    }


def write_record(path: Path, payload: dict) -> Path:
    """The record, written the way every other record in this project is."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


def refuse_missing_source(source: Path) -> Path:
    """Refuse a source file that is not there, with a code the GUI presents.

    Every verb that acts on a transcript someone else produced reads a path
    the caller supplied, and the most ordinary thing that can happen to such
    a path is that it stops resolving: the analysis folder was moved, the
    run was deleted, the client is holding a path from before either. That
    was reaching the client as `FileNotFoundError` -> an uncoded HTTP 500,
    which the GUI's table renders as 「未預期的錯誤」 -- the one presentation
    reserved for a code nobody has thought about.

    `usage_error` rather than a code of its own. `transcript.resolve`,
    `translate_document` and both `/v1/stack` path readers have answered
    "no such file" with it since they shipped, and
    `test_a_missing_file_is_a_usage_error_not_a_500` asserts it over HTTP; a
    second code for the same sentence would leave the GUI describing two
    things a reader cannot tell apart.

    Called BEFORE anything derived is computed, which is the second half of
    the defect: `runs.workspace_for` and `runs.open_run` create a folder for
    whatever they are handed, so a translation of a path that does not exist
    used to leave an empty analysis folder behind on its way to the 500.
    """
    if not source.is_file():
        raise UsageError(
            f"no such file: {source} -- it may have been moved or deleted. "
            f"Read the transcript again to get its current path"
        )
    return source


def read_source_text(source: Path, *, errors: str = "strict") -> str:
    """The text of a transcript, or `refuse_missing_source`'s refusal.

    `errors` is the caller's existing decision rather than a new one: the
    translate verbs have always decoded leniently (a `.srt` from anywhere
    may hold one bad byte, and a mojibake character is visible in the output
    where a crash is not), and `correct`/`tidy` have always decoded strictly.
    Unifying them would be a silent change to what those two write.
    """
    refuse_missing_source(source)
    return source.read_text(encoding="utf-8", errors=errors)


def read_cues(source: Path) -> list[dict]:
    """`parse_cues` over a file that has to be there.

    The four verbs over an existing transcript -- `correct` and `tidy`, each
    from the CLI and from its route -- all read their source exactly this
    way, and all four raised straight through on a missing file.
    """
    return parse_cues(read_source_text(source))


def refuse_self_overwrite(source: Path, target: Path) -> None:
    """Refuse to write a translation onto the file it was translated FROM.

    Reachable, and measured (F4 audit, 2026-08-30). Both translate verbs name
    their output after the TARGET language, so a source already named for a
    language can collide with its own output:
    `字幕檔/talk.eng_Latn.srt --from zh --to en` resolves to
    `字幕檔/talk.eng_Latn.srt`. The same-language check does not catch it --
    that reads the name, and `--from` overrides the name.

    `correct` and `tidy` cannot reach this: their outputs always gain a
    `.corrected` / `.tidy` infix, so the name can never equal the input's.
    Only the two verbs whose output name is a function of a FLAG need the
    guard, which is why it lives here rather than in `runs.place`.

    Raised, not renamed. A silent overwrite is worse than an error
    (CLAUDE.md), and picking a different name on the user's behalf would
    leave them with a file they did not ask for and no idea why.
    """
    try:
        same = target.resolve() == source.resolve()
    except OSError:  # pragma: no cover - unresolvable path, treat as distinct
        return
    if same:
        raise UsageError(
            f"that would write the translation onto {source.name} itself, "
            f"destroying the file it is being translated from. Rename the "
            f"source, or send the output elsewhere with --out"
        )


def runner_path() -> Path:
    """`asr/translate_runner.py`, in a checkout and in a frozen build.

    Same shape and same reasoning as `asr.runner_path`; `mfp.spec` ships
    both files to `asr/` under the bundle root, and if that entry is ever
    dropped this fails naming the path rather than reporting a missing model.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "asr" / "translate_runner.py"
    return Path(__file__).resolve().parents[2] / "asr" / "translate_runner.py"


def parse_cues(text: str) -> list[dict]:
    """`[{start, end, text}]` from SRT or WebVTT, timings intact.

    `stack.unique_lines` exists next door and is the wrong tool here: it
    de-duplicates rolling caption lines and keeps only a start, because what
    it feeds needs distinct LINES rather than faithful cues. Translation has
    to write a caption file back out, so it needs both ends of every cue and
    must not drop a repetition -- a line legitimately said twice is two cues.
    """
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln.rstrip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        index = 1 if lines[0].strip().isdigit() else 0
        stamps = list(TIMESTAMP.finditer(lines[index]))
        if len(stamps) != 2:
            continue
        body = " ".join(
            re.sub(r"\s+", " ", TAG.sub("", raw)).strip() for raw in lines[index + 1:]
        ).strip()
        if body:
            cues.append(
                {"start": seconds_of(stamps[0]), "end": seconds_of(stamps[1]), "text": body}
            )
    return cues


def translate_file(
    source: Path,
    *,
    out_dir: Path,
    python_exe: Path,
    model_dir: str,
    target: str,
    source_language: str | None = None,
    device: str = "auto",
    compute_type: str = "auto",
    say=None,
    on_progress=None,
) -> TranslationOutcome:
    """Translate one caption file and leave a caption file beside it.

    The result lands in `out_dir` -- the transcript's own run folder -- for
    the same reason a recognised transcript does: a file the user picked from
    anywhere is not ours to write next to, and our output root is always
    writable where a folder on a read-only share is not. Which subfolder of
    it depends on whether this is a `.srt` or a `.txt`, and `runs.place`
    decides that in one place for every writer.
    """
    say = say or (lambda _m: None)

    if source.suffix.lower() not in CAPTION_SUFFIXES:
        raise UsageError(
            f"{source.name} is not a transcript. This translates a caption "
            f"file (.srt/.vtt/.txt) that already exists -- run "
            f"`mfp transcript` on the media first"
        )
    raw = read_source_text(source, errors="replace")
    cues = parse_cues(raw)
    plain = not cues
    if plain:
        # A `.txt` transcript, or an SRT nothing could be parsed out of. Line
        # per line, and the output is a `.txt` -- inventing timings for it
        # would be inventing information.
        texts = [line.strip() for line in raw.splitlines() if line.strip()]
    else:
        texts = [cue["text"] for cue in cues]
    if not texts:
        raise UsageError(f"there is nothing to translate in {source.name}")

    runner = runner_path()
    if not runner.is_file():
        raise MfpError(f"the packaged translation runner is missing at {runner}")

    source_flores = flores_for(source_language) or _language_from_name(source)
    if source_flores is None:
        raise UsageError(
            "could not tell what language this transcript is in. Name it with "
            "--from, using an ISO code like `zh` or a FLORES code like "
            "`zho_Hant`"
        )
    target_flores = flores_for(target)
    if target_flores is None:
        raise UsageError(
            f"{target!r} is not a language this understands. Use an ISO code "
            f"like `en` or a FLORES code like `eng_Latn`"
        )
    if target_flores == source_flores:
        raise UsageError(
            f"the transcript is already in {source_flores}; translating it to "
            f"itself would just cost time"
        )

    say(f"translating {len(texts)} lines: {source_flores} -> {target_flores}")
    translated, payload, suspect, splits = translate_texts(
        runner,
        texts=texts,
        target_flores=target_flores,
        python_exe=python_exe,
        model_dir=model_dir,
        source_flores=source_flores,
        device=device,
        compute_type=compute_type,
        say=say,
        on_progress=on_progress,
    )
    if len(translated) != len(texts):
        raise MfpError(
            f"the translator returned {len(translated)} lines for {len(texts)} "
            f"-- refusing to write a transcript whose lines do not line up"
        )
    if suspect:
        say(
            f"{len(suspect)} line(s) may have lost a clause -- the engine "
            f"ended them mid-sentence. First: line {suspect[0] + 1}"
        )

    from mfp import runs

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _output_stem(source)
    name = f"{stem}.{target_flores}.{'txt' if plain else 'srt'}"
    # Judged on the name this translation WANTS, before `place_set` can dodge
    # the collision with a serial. A serial silently renaming the output is
    # the right answer to "I ran this twice"; it is the wrong answer to "this
    # would land on my source", which needs saying.
    refuse_self_overwrite(source, runs.place(out_dir, name))
    # ONE serial across the pair. Two `place_new` calls would happily file
    # `talk.eng_Latn.translation.json` next to a `talk.eng_Latn-2.srt` it does
    # not describe, which is worse than the overwrite it was avoiding (P-62).
    written = runs.place_set(
        out_dir, {"translation": name, "record": record_name(stem, target_flores)}
    )
    target_path = written["translation"]
    if plain:
        # A `.txt` transcript: no timings to keep, and inventing them would be
        # inventing information.
        target_path.write_text("\n".join(translated) + "\n", encoding="utf-8")
    else:
        target_path.write_text(
            to_srt([
                {"start": cue["start"], "end": cue["end"], "text": line}
                for cue, line in zip(cues, translated)
            ]),
            encoding="utf-8",
        )
    say(f"saved: {target_path}")

    outcome = TranslationOutcome(
        suspect_lines=tuple(suspect),
        source=target_path,
        target_language=target_flores,
        source_language=source_flores,
        line_count=len(translated),
        engine=payload.get("engine") or {},
        clause_splits=splits,
        record=written["record"],
    )
    write_record(written["record"], record(outcome, source=source))
    return outcome


def _output_stem(source: Path) -> str:
    """`talk.zh.srt` -> `talk`, so a chain of translations does not build a
    name like `talk.zh.eng_Latn.zho_Hant.srt`.

    Only a trailing component that LOOKS like a language tag is dropped; a
    file genuinely called `notes.v2.srt` keeps its `v2`.
    """
    stem = source.stem
    head, _, tail = stem.rpartition(".")
    if head and re.fullmatch(r"[A-Za-z]{2,3}([_-][A-Za-z]{2,4})?", tail):
        return head
    return stem


def _language_from_name(source: Path) -> str | None:
    """The language tag this project writes into its own caption filenames.

    `逐字稿/talk_2026-08-28_1432/字幕檔/talk.zh.srt` -- the tag is put there
    by `asr.recognize`, so reading it back is reading our own record rather
    than guessing. Returns `None` for anything else, and the caller then asks.
    """
    _head, _, tail = source.stem.rpartition(".")
    return flores_for(tail) if tail else None


def translate_texts(
    runner: Path,
    *,
    texts: list[str],
    target_flores: str,
    say=None,
    **runner_options,
) -> tuple[list[str], dict, list[int], int]:
    """`(translations, engine payload, truncated-looking indices, splits)`.

    One translation per input text, always -- the caller's counting is
    unaffected by what happens inside. What happens inside is that each text
    is cut into clauses first (`clauses_of`), because the engine loses whole
    clauses of a multi-clause sentence and does not lose them when they
    arrive separately.

    `splits` is how many extra pieces that cutting produced, and it is
    RETURNED rather than only said because it belongs in the record beside
    the output: it is the one number that says how far what the engine was
    asked stands from what the caller counted.

    Both verbs go through here. `translate_file` counts cues and
    `translate_document` counts sentences, and neither should have to know
    that the engine is asked for something finer than what it counts.
    """
    say = say or (lambda _m: None)

    pieces: list[str] = []
    owner: list[int] = []
    for index, text in enumerate(texts):
        parts = clauses_of(text) or [text]
        for part in parts:
            pieces.append(part)
            owner.append(index)

    split = len(pieces) - len(texts)
    if split:
        say(f"{split} clause split(s) so the engine keeps them")

    payload = run_translator(
        runner,
        texts=pieces,
        target_flores=target_flores,
        say=say,
        **runner_options,
    )
    lines = payload.get("lines") or []
    if len(lines) != len(pieces):
        raise MfpError(
            f"the translator returned {len(lines)} lines for {len(pieces)} "
            f"-- the pieces do not line up, so nothing built from them is "
            f"safe to write"
        )

    spaced = not joins_without_spaces(target_flores)
    joined = [""] * len(texts)
    for index, line in zip(owner, lines):
        joined[index] = _join_clauses(joined[index], line.strip(), spaced=spaced)

    suspect = [i for i, (a, b) in enumerate(zip(texts, joined)) if looks_truncated(a, b)]
    return joined, payload, suspect, split


def run_translator(
    runner: Path,
    *,
    python_exe: Path,
    model_dir: str,
    texts: list[str],
    source_flores: str,
    target_flores: str,
    device: str,
    compute_type: str,
    say,
    on_progress,
) -> dict:
    """Drive the runner. Same process discipline as `asr.recognize`.

    The lines go through a temporary FILE rather than argv: a transcript is
    routinely tens of kilobytes and Windows' command line is not, and the
    failure mode of exceeding it is a truncated argument rather than an
    error.
    """
    with tempfile.TemporaryDirectory(prefix="mfp-mt-") as tmp:
        request = Path(tmp) / "lines.json"
        request.write_text(
            json.dumps({"lines": texts}, ensure_ascii=False), encoding="utf-8"
        )
        argv = [
            str(python_exe), str(runner),
            "--model", model_dir,
            "--input", str(request),
            "--from", source_flores,
            "--to", target_flores,
            "--device", device,
            "--compute-type", compute_type,
        ]
        process = subprocess.Popen(
            argv,
            # DEVNULL for the reason every spawn site in this project uses
            # it: a child inheriting a stdin that another thread is blocked
            # reading is a child that hangs (measured 2026-08-17).
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        # BOTH pipes, concurrently. Same deadlock `asr.recognize` carried and
        # for the same reason, except worse here: the result is every
        # translated line of the transcript, so this one overflows a Windows
        # pipe sooner than recognition does. Draining stderr to EOF first
        # cannot work -- EOF arrives when the child exits, and the child
        # cannot exit while blocked writing stdout.
        assert process.stdout is not None
        assert process.stderr is not None
        collected: list[str] = []

        def _drain_stdout() -> None:
            collected.append(process.stdout.read())

        reader = threading.Thread(target=_drain_stdout, daemon=True)
        reader.start()

        for line in process.stderr:
            line = line.rstrip("\r\n")
            if line.startswith("@mt "):
                if on_progress is not None:
                    try:
                        on_progress(json.loads(line[4:]))
                    except json.JSONDecodeError:
                        pass
                continue
            if line:
                say(line)

        reader.join()
        process.wait()

    payload = _last_json_object("".join(collected))
    # A complete result outranks the exit code, for the reason spelled out in
    # `asr.recognize`: CTranslate2's CUDA teardown can kill this process after
    # the work is done, and every real failure writes `ok: false` first.
    if not payload.get("ok"):
        message = payload.get("error") or (
            f"the translator exited {process.returncode} without saying why"
        )
        if process.returncode == EXIT_UNUSABLE:
            raise TranslationUnavailable(message)
        raise MfpError(message)
    if process.returncode != 0:
        say(f"note: the translator finished and then exited "
            f"{process.returncode} on the way out; the result below is its own")
    return payload
