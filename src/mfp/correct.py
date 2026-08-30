"""Post-hoc term correction: propose, disclose, apply -- in that order.

D-115 refuted the version of this that took its terms FROM the transcript, and
the refutation is worth restating because it is the reason for every rule in
this module. Voting between spellings needs two spellings of one term in one
file; the drift is across RUNS, and a user holds one transcript. What the
clustering actually found on real text was homophone collisions between
DIFFERENT words, and it applied them: 任何事情 became 任合適情, because 何事
lost a majority vote to 合適.

The fix is not a better score. It is a smaller candidate set. A maintained
glossary is safe because it is a WHITELIST: 合適 cannot win because it is not
in the set at all. Nothing here can substitute a word the user has not
declared, so the worst case is a term the user asked for appearing where they
did not want it -- visible, listed, and undoable -- rather than ordinary prose
quietly rewriting itself.

Three properties this module is built to guarantee, stated as properties of
the artifact rather than as reminders:

  1. `apply` may only SUBSTITUTE inside a span. Cue count and every timestamp
     are identical before and after, asserted, or it raises. Deletion is the
     one defect class this project cannot see downstream (P-50), so it is
     refused at the type level rather than checked for afterwards.
  2. A proposal that is not in the glossary cannot exist. An empty glossary
     produces an empty proposal list -- the negative control is structural.
  3. A tie keeps the original (D-112's rule, reused). Equal keys, equal
     distance, missing evidence: the incumbent stands.

The correction the user makes BY HAND is the interesting half. Enrolling it
turns the wrong form into an `alias`, and the next run matches it exactly
instead of guessing at its reading. The accuracy of this feature comes from
use, not from a model.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mfp.errors import MfpError

#: Where a proposal is allowed to come from.
TIER_EXACT = "exact"       # the transcript contains a form the user enrolled
TIER_PHONETIC = "phonetic"  # it merely SOUNDS like a term the user declared

#: A word the decoder scored above this was not a word it was unsure about, so
#: a phonetic guess has no business overruling it. A payload with no
#: word-level evidence leaves every confidence None, and tier B then declines
#: rather than firing blind.
#:
#: TWO numbers, because one would be a lie about one of the two scripts.
#: Measured over 1,190 words from nine arms of known text
#: (`docs/spike-06-post-correction.md` E1):
#:
#:     CJK    AUC 0.951   at < 0.70: catches 77% of wrong words, 4.2% cost
#:     Latin  AUC 0.757   at < 0.80: catches 50% of wrong words, 18.1% cost
#:
#: An English term inside a Chinese decode is scored low whether or not it is
#: right -- `feature` at 0.040 and `paper` at 0.109 were both CORRECT -- so
#: the pooled figure (0.926) flatters the Latin case by drowning it in the
#: Chinese one. Splitting them is what makes either number usable.
CONFIDENT_ENOUGH = {"zh": 0.70, "en": 0.80}

#: What the gate cannot see, stated so nobody has to rediscover it: the words
#: this measurement found HIGHEST-confidence and wrong were hallucinated
#: boilerplate (`吝` at 0.998, `目` at 0.999 -- a YouTube outro the engine
#: invented after the recording ended). Confidence finds substitutions, which
#: is what this module corrects; it is blind to invention, which is not.

#: Latin runs, the unit an English term drifts within (`baseline` ->
#: `faceline`). Digits and the inner hyphen are included because `V-glue` and
#: `P-Value` are one term and not two -- P-51 lost a term to a scorer that
#: split them.
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*")

GLOSSARY_FILE = "_glossary.json"
SCHEMA_VERSION = 1


class CorrectionRefused(Exception):
    """A patch that cannot be applied to this transcript. Never a warning: an
    edit list applied to the wrong file is a silent corruption."""


class GlossaryConflict(MfpError):
    """An edit that would make two entries into one. Refused rather than
    resolved: which of the two alias lists survives is the user's call, and
    a merge nobody asked for is a deletion nobody sees.

    An `MfpError` rather than a bare exception because it travels: its
    message is user-facing prose that `/v1/glossary:update` shows verbatim,
    and 409 is the status that already means "your edit collided with what
    is there" in this server's table.
    """

    error_code = "glossary_conflict"
    exit_code = 2


@dataclass(frozen=True)
class Entry:
    """One declared term.

    `term` is the correct form. `aliases` are wrong forms that have actually
    been seen -- they come from the user's own corrections, which is what makes
    them exact rather than guessed.
    """

    term: str
    aliases: tuple[str, ...] = ()
    lang: str = "auto"
    note: str = ""

    def as_dict(self) -> dict:
        row = {"term": self.term}
        if self.aliases:
            row["aliases"] = list(self.aliases)
        if self.lang != "auto":
            row["lang"] = self.lang
        if self.note:
            row["note"] = self.note
        return row

    @property
    def language(self) -> str:
        if self.lang != "auto":
            return self.lang
        return "en" if LATIN_RUN.fullmatch(self.term.replace(" ", "")) else "zh"


@dataclass(frozen=True)
class Proposal:
    """One substitution, offered. Never applied by existing."""

    cue: int
    start: int
    end: int
    was: str
    now: str
    term: str
    tier: str
    key: str = ""
    confidence: float | None = None

    def as_dict(self) -> dict:
        return {"cue": self.cue, "start": self.start, "end": self.end,
                "was": self.was, "now": self.now, "term": self.term,
                "tier": self.tier, "key": self.key,
                "confidence": self.confidence}

    @property
    def line(self) -> str:
        where = f"cue {self.cue + 1}"
        how = "登記過的寫法" if self.tier == TIER_EXACT else "讀音相同"
        sure = "" if self.confidence is None else f"，引擎信心 {self.confidence:.0%}"
        return f"{where}: {self.was} → {self.now}（{how}{sure}）"


# --------------------------------------------------------------------------
# Phonetic keys. One per language, because the error classes do not share a
# reading: 機板/基板 is a homophone and baseline/faceline is a misspelling, and
# a key that tries to serve both serves neither.
# --------------------------------------------------------------------------

def _pinyin_key(text: str) -> str | None:
    """Toneless pinyin, or None when the reading cannot be obtained.

    None is not an error. `pypinyin` is optional: without it this module still
    does tier A, which is the tier the user's own corrections feed. A feature
    that degrades to "exact matches only" is worth shipping; one that raises on
    a missing optional import is not.
    """
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return None
    syllables = lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
    return " ".join(syllables) if syllables else None


def pinyin_available() -> bool:
    return _pinyin_key("基板") is not None


#: One reading per CHARACTER, cached. Two reasons it is per character rather
#: than per window: the index has to line up with the string so a hit can be
#: reported as a span, and a two-hour transcript scanned window-by-window
#: against twenty terms would ask for the same reading a hundred thousand
#: times. Unique characters in Chinese are in the low thousands.
_READING: dict[str, str] = {}


def _syllables(text: str) -> list[str]:
    out = []
    for char in text:
        reading = _READING.get(char)
        if reading is None:
            reading = "" if unicodedata.category(char) != "Lo" else (_pinyin_key(char) or "")
            _READING[char] = reading
        out.append(reading)
    return out


def _latin_key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _distance(left: str, right: str) -> int:
    """Levenshtein, written out. This module may not add a dependency to
    compare two short strings."""
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1,
                               current[j - 1] + 1,
                               previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _latin_tolerance(term: str) -> int:
    """How far a Latin term may drift and still be recognised as that term.

    Length-scaled, and never more than a quarter of the word. `run` and `tag`
    get zero, which is deliberate: a three-letter term one edit from another
    three-letter term is not a correction, it is a coin toss.
    """
    return len(_latin_key(term)) // 4


# --------------------------------------------------------------------------
# Glossary
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Glossary:
    """The whitelist. Everything this module may write comes from here."""

    entries: tuple[Entry, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entries)

    @classmethod
    def empty(cls) -> "Glossary":
        return cls(())

    @classmethod
    def from_dict(cls, payload: dict) -> "Glossary":
        rows = payload.get("entries", []) if isinstance(payload, dict) else []
        entries = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("term"):
                continue
            entries.append(Entry(
                term=str(row["term"]),
                aliases=tuple(str(a) for a in row.get("aliases", []) if a),
                lang=str(row.get("lang", "auto")),
                note=str(row.get("note", "")),
            ))
        return cls(tuple(entries))

    @classmethod
    def load(cls, path: Path) -> "Glossary":
        path = Path(path)
        if not path.exists():
            return cls.empty()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A corrupt glossary must not take the transcript down with it.
            # Empty means "no proposals", which is the safe direction.
            return cls.empty()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schemaVersion": SCHEMA_VERSION,
                        "entries": [e.as_dict() for e in self.entries]},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")

    def enrol(self, term: str, alias: str = "") -> "Glossary":
        """Add a term, or attach a newly-seen wrong form to one already here.

        This is what a hand correction becomes. `alias` is the form that was
        actually written, so the next run does not have to guess a reading --
        the same drift is matched exactly.
        """
        term = term.strip()
        alias = alias.strip()
        if not term or alias == term:
            alias = ""
        if not term:
            return self
        rows = list(self.entries)
        for index, entry in enumerate(rows):
            if entry.term == term:
                if alias and alias not in entry.aliases:
                    rows[index] = Entry(entry.term, entry.aliases + (alias,),
                                        entry.lang, entry.note)
                return Glossary(tuple(rows))
        rows.append(Entry(term, (alias,) if alias else ()))
        return Glossary(tuple(rows))

    def update(self, original: str, entry: Entry) -> "Glossary":
        """Replace the entry called `original`, keeping its position.

        The half `enrol` never had. A glossary that can only grow is a
        glossary whose first typo is permanent -- and this one decides what
        the corrector is allowed to write, so a wrong entry there is a wrong
        word in every transcript afterwards (user report 2026-08-28).

        Renaming onto a term that already exists is REFUSED rather than
        merged. Two entries silently becoming one is a deletion, and the
        user is the only one who knows which of the two aliases lists they
        meant to keep.
        """
        original = original.strip()
        rows = list(self.entries)
        at = next((i for i, row in enumerate(rows) if row.term == original), None)
        if at is None:
            return self.add(entry)
        clash = next(
            (i for i, row in enumerate(rows) if row.term == entry.term and i != at),
            None,
        )
        if clash is not None:
            raise GlossaryConflict(
                f"詞庫裡已經有「{entry.term}」了。要合併的話，"
                f"請先把其中一筆的錯字搬到另一筆，再刪掉多出來的那筆"
            )
        rows[at] = entry
        return Glossary(tuple(rows))

    def add(self, entry: Entry) -> "Glossary":
        """Append an entry, whole. `enrol` is the one-alias shorthand."""
        if not entry.term.strip():
            return self
        if any(row.term == entry.term for row in self.entries):
            raise GlossaryConflict(f"詞庫裡已經有「{entry.term}」了")
        return Glossary(self.entries + (entry,))

    def remove(self, term: str) -> "Glossary":
        """Drop an entry. Removing one that is not there is not an error --
        the caller wanted it gone and it is gone."""
        term = term.strip()
        return Glossary(tuple(row for row in self.entries if row.term != term))

    def fingerprint(self) -> str:
        payload = json.dumps([e.as_dict() for e in self.entries],
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def glossary_path(output_root: str | Path) -> Path:
    return Path(output_root).expanduser() / GLOSSARY_FILE


# --------------------------------------------------------------------------
# Proposing
# --------------------------------------------------------------------------

def _confidence_at(cue: dict, start: int, end: int) -> float | None:
    """The engine's own probability over the characters being replaced.

    Word probabilities are positional in TIME, not in the cue string, so the
    span is mapped by walking the words in order and counting characters. When
    a payload has no word evidence the answer is None, and None is not zero:
    a caller that treats "not measured" as "unsure" would fire everywhere on
    an older payload.
    """
    words = cue.get("words")
    if not words:
        return None

    # The words concatenate to the segment text the ENGINE produced; the cue
    # carries that text stripped. Whisper's word tokens keep their leading
    # space (" feature"), so on a segment that begins with an English word the
    # two differ by one character and every offset below would be shifted by
    # one -- reading the confidence of the neighbouring word. Measured across
    # 145 real segments the two happen to agree every time, which is exactly
    # the kind of agreement that stops holding on input nobody has seen yet.
    joined = "".join(str(word.get("word", "")) for word in words)
    text = str(cue.get("text", ""))
    shift = joined.find(text) if text else 0
    if shift < 0:
        shift = len(joined) - len(joined.lstrip())

    at = 0
    hits: list[float] = []
    for word in words:
        length = len(str(word.get("word", "")))
        first, last = at - shift, at + length - shift
        at += length
        if last <= start or first >= end:
            continue
        probability = word.get("prob")
        if probability is not None:
            hits.append(float(probability))
    if not hits:
        return None
    return min(hits)


def _exact_hits(text: str, entry: Entry) -> list[tuple[int, int, str]]:
    found = []
    for alias in entry.aliases:
        if not alias:
            continue
        at = text.find(alias)
        while at != -1:
            found.append((at, at + len(alias), alias))
            at = text.find(alias, at + 1)
    return found


def _phonetic_hits_zh(
    text: str, entry: Entry, want: list[str], readings: list[str],
) -> list[tuple[int, int, str]]:
    """Same length, same reading, different characters.

    Length is not a nicety. Matching a two-character term against a
    three-character window is how a correction starts eating neighbouring
    words, and 任合適情 is what that looks like when it ships.
    """
    width = len(entry.term)
    if width < 2:
        # A single character has too many homophones to be worth guessing at.
        return []
    found = []
    for start in range(0, max(0, len(text) - width + 1)):
        end = start + width
        if readings[start:end] != want:
            continue
        window = text[start:end]
        if window != entry.term:
            found.append((start, end, window))
    return found


def _phonetic_hits_en(text: str, entry: Entry) -> list[tuple[int, int, str]]:
    target = _latin_key(entry.term)
    tolerance = _latin_tolerance(entry.term)
    if tolerance < 1:
        return []
    found = []
    for match in LATIN_RUN.finditer(text):
        word = match.group(0)
        if word == entry.term:
            continue
        candidate = _latin_key(word)
        if not candidate or abs(len(candidate) - len(target)) > tolerance:
            continue
        if _distance(candidate, target) <= tolerance:
            found.append((match.start(), match.end(), word))
    return found


def propose(
    cues: list[dict],
    glossary: Glossary,
    *,
    confident_enough: dict[str, float] | None = None,
    allow_phonetic: bool = True,
) -> list[Proposal]:
    """Everything that COULD be corrected, with the reason attached.

    Pure: cues in, proposals out. No engine, no model, no audio, no file
    system -- the same shape as `quality.inspect_cues`, and for the same
    reason. It runs on a caption track from anywhere, and it is testable
    without any of the machinery that produced the transcript.

    Nothing is applied here. `apply` is a separate call because the user has
    to see the list first: that is the ruling this feature was rebuilt under.
    """
    if not glossary:
        return []
    ceiling = dict(CONFIDENT_ENOUGH)
    ceiling.update(confident_enough or {})
    proposals: list[Proposal] = []
    wanted = {entry.term: _syllables(entry.term) for entry in glossary.entries}
    keys = {term: " ".join(s for s in syllables if s)
            for term, syllables in wanted.items()}
    for index, cue in enumerate(cues):
        text = str(cue.get("text", ""))
        if not text:
            continue
        taken: list[tuple[int, int]] = []

        def _free(start: int, end: int) -> bool:
            return not any(start < b and a < end for a, b in taken)

        # Tier A first, and it wins any overlap: a form the user actually
        # enrolled outranks one this module merely thinks sounds similar.
        for entry in glossary.entries:
            for start, end, was in _exact_hits(text, entry):
                if not _free(start, end):
                    continue
                taken.append((start, end))
                proposals.append(Proposal(
                    cue=index, start=start, end=end, was=was, now=entry.term,
                    term=entry.term, tier=TIER_EXACT,
                    confidence=_confidence_at(cue, start, end)))

        if not allow_phonetic:
            continue

        readings = _syllables(text)
        for entry in glossary.entries:
            language = entry.language
            if language == "zh":
                want = wanted.get(entry.term) or []
                hits = (_phonetic_hits_zh(text, entry, want, readings)
                        if all(want) else [])
            else:
                hits = _phonetic_hits_en(text, entry)
            for start, end, was in hits:
                if not _free(start, end):
                    continue
                confidence = _confidence_at(cue, start, end)
                # The evidence gate. A word the engine was sure about is not a
                # word a phonetic guess may overrule -- and "not measured" is
                # not "unsure", so a payload without word evidence declines.
                if confidence is None or confidence >= ceiling.get(language, 0.80):
                    continue
                taken.append((start, end))
                proposals.append(Proposal(
                    cue=index, start=start, end=end, was=was, now=entry.term,
                    term=entry.term, tier=TIER_PHONETIC,
                    key=keys.get(entry.term) or _latin_key(entry.term),
                    confidence=confidence))
    proposals.sort(key=lambda p: (p.cue, p.start))
    return proposals


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def apply(cues: list[dict], proposals: list[Proposal]) -> list[dict]:
    """Substitute, and prove that nothing else happened.

    The assertions are the point. Every other guarantee in this module is a
    matter of what the code intends; these are checked against the result,
    because the failure they guard is the one nothing downstream can see.
    """
    edits: dict[int, list[Proposal]] = {}
    for proposal in proposals:
        if not 0 <= proposal.cue < len(cues):
            # Refused rather than skipped. A patch naming a cue this
            # transcript does not have was written for a different file, and
            # quietly applying the rest of it is how half a correction lands
            # on a transcript nobody checked.
            raise CorrectionRefused(
                f"this patch edits cue {proposal.cue + 1}, and the transcript "
                f"has {len(cues)}. It was written for a different file")
        edits.setdefault(proposal.cue, []).append(proposal)

    out: list[dict] = []
    for index, cue in enumerate(cues):
        row = dict(cue)
        for proposal in sorted(edits.get(index, []), key=lambda p: -p.start):
            text = str(row.get("text", ""))
            if text[proposal.start:proposal.end] != proposal.was:
                raise CorrectionRefused(
                    f"cue {index + 1} no longer reads {proposal.was!r} at "
                    f"{proposal.start}. This patch was written for a "
                    f"different version of the transcript")
            row["text"] = text[:proposal.start] + proposal.now + text[proposal.end:]
        out.append(row)

    if len(out) != len(cues):
        raise CorrectionRefused("correction changed the number of cues")
    for before, after in zip(cues, out):
        if before.get("start") != after.get("start") or before.get("end") != after.get("end"):
            raise CorrectionRefused("correction moved a timestamp")
    return out


def fingerprint(cues: list[dict]) -> str:
    payload = json.dumps([{"start": c.get("start"), "end": c.get("end"),
                           "text": c.get("text")} for c in cues],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Disclosure -- the half the user rules on
# --------------------------------------------------------------------------

def diff(cues: list[dict], proposals: list[Proposal]) -> str:
    """What changed, per cue, in the form a person can check line by line.

    Not a unified diff. A transcript diff that shows two nearly-identical
    Chinese sentences on adjacent lines is unreadable at exactly the moment it
    matters; naming the span is what makes it checkable.
    """
    if not proposals:
        return "沒有可以校正的地方。"
    by_cue: dict[int, list[Proposal]] = {}
    for proposal in proposals:
        by_cue.setdefault(proposal.cue, []).append(proposal)
    lines: list[str] = []
    for index in sorted(by_cue):
        before = str(cues[index].get("text", ""))
        after = str(apply(cues, by_cue[index])[index].get("text", ""))
        lines.append(f"--- 第 {index + 1} 句")
        lines.append(f"-   {before}")
        lines.append(f"+   {after}")
        for proposal in by_cue[index]:
            lines.append(f"    · {proposal.line}")
    exact = sum(1 for p in proposals if p.tier == TIER_EXACT)
    lines.append("")
    lines.append(f"共 {len(proposals)} 處：{exact} 處是登記過的寫法，"
                 f"{len(proposals) - exact} 處是靠讀音判斷的（請逐項確認）。")
    return "\n".join(lines)


def patch(
    cues: list[dict],
    proposals: list[Proposal],
    *,
    source: str,
    glossary: Glossary,
    accepted: list[int] | None = None,
) -> dict:
    """The second of the two copies: what changed, why, and against what.

    `sourceSha256` is what makes this refusable rather than best-effort. A
    patch carried to a different transcript is a corruption waiting to happen,
    so it is bound to the exact text it was computed from.
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "sourceSha256": fingerprint(cues),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "glossary": {"fingerprint": glossary.fingerprint(),
                     "entries": len(glossary.entries)},
        "phoneticKeys": pinyin_available(),
        "proposals": [p.as_dict() for p in proposals],
        "accepted": list(range(len(proposals))) if accepted is None else accepted,
    }


def _stem(source: Path) -> str:
    """`talk.zh.srt` -> `talk.zh`. One suffix, not all of them: the language
    tag is part of the identity, and `talk.corrected.srt` sitting next to
    `talk.en.srt` and `talk.zh.srt` would belong to neither."""
    return source.name[: -len(source.suffix)] if source.suffix else source.name


def write_pair(
    source: Path,
    cues: list[dict],
    proposals: list[Proposal],
    *,
    glossary: Glossary,
    out_dir: Path | None = None,
    accepted: list[int] | None = None,
) -> dict[str, Path]:
    """The two copies, plus the readable form of the second one.

    The original is not touched. That is the ruling this feature was rebuilt
    under, and it is also the only reason the patch is safe to be wrong about:
    a correction the user dislikes costs them a file they can delete, not the
    transcript they already had.
    """
    from mfp import runs
    from mfp.asr import to_srt, to_text

    source = Path(source)
    if out_dir:
        out_dir = Path(out_dir)
    else:
        # The analysis this transcript belongs to, not the folder the file
        # happens to sit in: the source is normally inside `字幕檔/`, and
        # writing the corrected copy there would bury it one level below the
        # run it belongs to -- and put the `.txt` in the subtitles folder.
        found = runs.run_of(source)
        out_dir = found.root if found else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _stem(source)

    keep = proposals if accepted is None else [proposals[i] for i in accepted]
    corrected = apply(cues, keep)

    # One serial across all four (`runs.place_set`): a corrections record
    # filed under a different serial than the transcript it describes would
    # be a record of nothing.
    written = runs.place_set(out_dir, {
        "corrected": f"{stem}.corrected.srt",
        "reading": f"{stem}.corrected.txt",
        "record": f"{stem}.corrections.json",
        "diff": f"{stem}.corrections.diff.txt",
    })
    written["corrected"].write_text(to_srt(corrected), encoding="utf-8")

    written["reading"].write_text(
        to_text(str(c.get("text", "")) for c in corrected), encoding="utf-8")

    written["record"].write_text(
        json.dumps(patch(cues, proposals, source=source.name, glossary=glossary,
                         accepted=accepted),
                   ensure_ascii=False, indent=1),
        encoding="utf-8")

    written["diff"].write_text(diff(cues, keep) + "\n", encoding="utf-8")
    return written


def from_patch(payload: dict) -> list[Proposal]:
    rows = payload.get("proposals", [])
    accepted = payload.get("accepted")
    keep = set(range(len(rows))) if accepted is None else set(accepted)
    out = []
    for index, row in enumerate(rows):
        if index not in keep:
            continue
        out.append(Proposal(
            cue=int(row["cue"]), start=int(row["start"]), end=int(row["end"]),
            was=str(row["was"]), now=str(row["now"]), term=str(row.get("term", "")),
            tier=str(row.get("tier", TIER_EXACT)), key=str(row.get("key", "")),
            confidence=row.get("confidence")))
    return out
