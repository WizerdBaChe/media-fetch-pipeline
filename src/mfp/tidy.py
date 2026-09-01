"""The reading copy: a transcript with its filler cues taken out.

Same three-step shape as `correct` -- propose, disclose, apply -- and for the
same reason. What is different is that this DELETES, and deletion is the one
defect class this project cannot see downstream (P-50, P-59). So the rules
here are stricter than the corrector's, not looser.

**It removes whole cues and nothing else.** Measured on a real 158-cue
transcript from this machine before any of this was written:

    token   occurrences   whole cue   inside a sentence
    嗯               13          13                   0
    好               46          23                  18
    那個             28           0                  28
    就是             13           0                  13

The whole-cue column is unambiguous: a cue that says only 嗯 says nothing
else. The other column is where meaning lives -- of the 18 embedded 好, seven
are content (角度調好, 一個好像, 做的好不好, 我剛好明天), and all 28 的
那個 are demonstratives naming real things (那個凹凸鏡, 那個玻璃機板). A
string-match pass over those would not tidy the transcript, it would damage
it. Removing them needs to know what a word is DOING, which needs a tagger or
a language model; the corrector deferred an n-gram model for the same reason
and named the same trigger to revisit. `is_filler_cue` is the swap point.

One measurement worth recording because it refuted the obvious design: the
rule "a filler is cue-initial and followed by a comma" matched **zero** cues
in that transcript. The intuition was wrong and the data said so.

**The candidate set is the user's list** (D-116's rule, reused). An empty
list removes nothing, and that is structural rather than tuned -- nothing
here can decide a word is a filler on its own.

**The original is never touched**, and the record can rebuild it. `apply`
asserts `restore(kept, removals) == cues` before anything is written, so
"only declared fillers were removed" is proved against the result rather
than intended by the code.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mfp import runs
from mfp.errors import MfpError

__all__ = [
    "COMMON_FILLERS",
    "FILLERS_FILE",
    "FillerList",
    "Removal",
    "TidyRefused",
    "apply",
    "fillers_path",
    "is_filler_cue",
    "propose",
    "restore",
    "summary",
    "write_pair",
]

FILLERS_FILE = "_fillers.json"
SCHEMA_VERSION = 1


class TidyRefused(MfpError):
    """The tidied copy would not have been a subset of the original.

    Exit 3 with the rest of the "this input cannot be used" family. Raised
    rather than repaired: every path that reaches it means the result and
    the record disagree, and a reading copy nobody can trace back to its
    transcript is worse than no reading copy.
    """

    error_code = "tidy_refused"
    exit_code = 3


#: Punctuation and spacing that can surround a filler without changing that
#: it is one. Deliberately not "all punctuation": a cue reading `好?` is a
#: question and the question mark is the only thing saying so, so `?` and
#: `？` are absent and such a cue is kept.
_TRIM = " \t　,.、，。;；:：!！~～-—…"

#: Fillers common enough to be worth offering, so the feature is usable
#: without the user having to think of the list themselves. NOT a default:
#: nothing is removed until these are added to the user's own file, which is
#: what `--add-common` does and what makes it their decision.
#:
#: Every entry here is safe ONLY under the whole-cue rule. `好` and `對` are
#: on the list because a cue that is nothing but 好 is an acknowledgement;
#: the same two characters inside a sentence are ordinary words and this
#: module never touches those.
COMMON_FILLERS: tuple[str, ...] = (
    "嗯", "呃", "啊", "喔", "噢", "欸", "唔", "哦",
    "好", "對", "是", "那個", "就是", "然後", "所以說",
    "um", "uh", "er", "mm", "hmm", "okay", "ok", "right", "yeah",
)


def _normalise(text: str) -> str:
    """Fold width and case so `ＯＫ`, `OK` and `ok` are one term.

    NFKC because a transcript can carry full-width Latin from an IME, and a
    user who typed the half-width form in their list means both.
    """
    return unicodedata.normalize("NFKC", text).strip().casefold()


@dataclass(frozen=True)
class FillerList:
    """The whitelist. Nothing outside it can be removed from anything."""

    terms: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.terms)

    def __len__(self) -> int:
        return len(self.terms)

    @classmethod
    def empty(cls) -> "FillerList":
        return cls(())

    @classmethod
    def from_dict(cls, payload: dict) -> "FillerList":
        rows = payload.get("terms", []) if isinstance(payload, dict) else []
        seen: list[str] = []
        for row in rows:
            term = str(row).strip() if row else ""
            if term and term not in seen:
                seen.append(term)
        return cls(tuple(seen))

    @classmethod
    def load(cls, path: Path) -> "FillerList":
        path = Path(path)
        if not path.exists():
            return cls.empty()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Same direction as the glossary's: a corrupt list means "remove
            # nothing", never "remove everything".
            return cls.empty()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"schemaVersion": SCHEMA_VERSION, "terms": list(self.terms)},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    def add(self, *terms: str) -> "FillerList":
        rows = list(self.terms)
        for term in terms:
            term = term.strip()
            if term and term not in rows:
                rows.append(term)
        return FillerList(tuple(rows))

    def remove(self, *terms: str) -> "FillerList":
        """Drop terms. Removing one that is not there is not an error -- the
        caller wanted it gone and it is gone."""
        drop = {t.strip() for t in terms}
        return FillerList(tuple(t for t in self.terms if t not in drop))

    def fingerprint(self) -> str:
        payload = json.dumps(sorted(self.terms), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fillers_path(output_root: str | Path) -> Path:
    """`<outputRoot>/_reference/_fillers.json`.

    Class D (D-142), for the same reason as the glossary: the list is the
    user's, it governs what may be deleted from a reading copy, and it does
    not belong among rough output. Adoption from the old flat location
    copies once and deletes nothing (`runs.adopt_reference`).
    """
    return runs.adopt_reference(output_root, FILLERS_FILE)


@dataclass(frozen=True)
class Removal:
    """One cue that would go, and everything needed to put it back."""

    cue: int
    text: str
    start: float
    end: float

    def as_dict(self) -> dict:
        return {"cue": self.cue, "text": self.text, "start": self.start,
                "end": self.end}


def is_filler_cue(text: str, fillers: FillerList) -> bool:
    """Is this cue nothing but declared fillers?

    True only when the whole cue can be consumed by declared terms and the
    punctuation between them -- so `嗯`, `嗯嗯`, `好好好` and `嗯，好` are
    fillers, and `好不好`, `好像` and `那個凹凸鏡` are not, because something
    is left over.

    Never a substring test. `好` matching inside `好像` is the failure this
    whole module is arranged to avoid, and the arrangement is that the cue
    must be exhausted, not merely touched.
    """
    if not fillers:
        return False
    rest = _normalise(text).strip(_TRIM)
    if not rest:
        return False
    # Longest first, so `那個` is tried before a hypothetical `那`.
    terms = sorted((_normalise(t) for t in fillers.terms if t.strip()), key=len,
                   reverse=True)
    if not terms:
        return False
    while rest:
        for term in terms:
            if rest.startswith(term):
                rest = rest[len(term):].strip(_TRIM)
                break
        else:
            return False
    return True


def propose(cues: list[dict], fillers: FillerList) -> list[Removal]:
    """Which cues a tidied copy would leave out.

    An empty list proposes nothing, and no argument can change that: the
    negative control is that there is no code path from "not in the list" to
    "removed".
    """
    out: list[Removal] = []
    for index, cue in enumerate(cues):
        text = str(cue.get("text", ""))
        if is_filler_cue(text, fillers):
            out.append(Removal(
                cue=index,
                text=text,
                start=float(cue.get("start") or 0.0),
                end=float(cue.get("end") or 0.0),
            ))
    return out


def apply(
    cues: list[dict], removals: list[Removal], *, fillers: FillerList
) -> list[dict]:
    """The cues that survive, and proof that nothing else changed.

    Three checks, in the order that makes a failure legible:

    1. every removal names a cue this transcript has, once;
    2. every removed cue is STILL nothing but declared fillers -- re-derived
       here rather than trusted from `propose`, so a hand-edited record
       cannot delete a sentence;
    3. putting the removals back reproduces the input exactly.

    Check 3 is the one that matters. It is a statement about the RESULT
    rather than about the code's intent, and it is what makes "only declared
    fillers were removed" a fact instead of a claim.
    """
    seen: set[int] = set()
    for removal in removals:
        if not 0 <= removal.cue < len(cues):
            raise TidyRefused(
                f"this record removes cue {removal.cue + 1}, and the "
                f"transcript has {len(cues)}. It was written for a "
                f"different file"
            )
        if removal.cue in seen:
            raise TidyRefused(f"cue {removal.cue + 1} is removed twice")
        seen.add(removal.cue)

        actual = str(cues[removal.cue].get("text", ""))
        if actual != removal.text:
            raise TidyRefused(
                f"cue {removal.cue + 1} no longer reads {removal.text!r}. "
                f"This record was written for a different version of the "
                f"transcript"
            )
        if not is_filler_cue(actual, fillers):
            raise TidyRefused(
                f"cue {removal.cue + 1} ({actual!r}) is not made only of "
                f"terms in the filler list, so removing it would delete "
                f"something nobody declared"
            )

    kept = [dict(cue) for index, cue in enumerate(cues) if index not in seen]

    rebuilt = restore(kept, removals)
    if len(rebuilt) != len(cues) or any(
        a.get("text") != b.get("text")
        or a.get("start") != b.get("start")
        or a.get("end") != b.get("end")
        for a, b in zip(rebuilt, cues)
    ):
        raise TidyRefused(
            "the tidied copy plus its record does not rebuild the original "
            "transcript; refusing to write it"
        )
    return kept


def restore(kept: list[dict], removals: list[Removal]) -> list[dict]:
    """The original, from the tidied copy and the record.

    Exists so `apply` can prove itself, and so a user who wants the removed
    lines back has a defined way to get them rather than a diff to read.
    """
    out: list[dict] = []
    by_index = {r.cue: r for r in removals}
    total = len(kept) + len(removals)
    remaining = list(kept)
    for index in range(total):
        removal = by_index.get(index)
        if removal is not None:
            out.append({"start": removal.start, "end": removal.end,
                        "text": removal.text})
        elif remaining:
            out.append(remaining.pop(0))
    return out


def summary(cues: list[dict], removals: list[Removal]) -> dict:
    """The numbers a caller reports, computed in one place so the CLI, the
    JSON and the GUI cannot disagree about them."""
    removed_chars = sum(len(r.text) for r in removals)
    total_chars = sum(len(str(c.get("text", ""))) for c in cues)
    return {
        "cues": len(cues),
        "removed": len(removals),
        "kept": len(cues) - len(removals),
        "removedChars": removed_chars,
        "totalChars": total_chars,
        "share": round(len(removals) / len(cues), 3) if cues else 0.0,
    }


def record(
    cues: list[dict],
    removals: list[Removal],
    *,
    source: str,
    fillers: FillerList,
    offered: int | None = None,
) -> dict:
    """The machine-readable half: what went, and enough to put it back.

    `offered` is how many removals were PROPOSED when the user only accepted
    some. It differs from `summary.removed` exactly when somebody kept a cue
    the rules would have dropped, and that difference is worth keeping: it is
    the record of a human disagreeing with the filler list.
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fillers": list(fillers.terms),
        "fillersFingerprint": fillers.fingerprint(),
        "offered": len(removals) if offered is None else offered,
        "summary": summary(cues, removals),
        "removals": [r.as_dict() for r in removals],
    }


def from_record(payload: dict) -> list[Removal]:
    rows = payload.get("removals", []) if isinstance(payload, dict) else []
    out: list[Removal] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("cue"), int):
            continue
        out.append(Removal(
            cue=int(row["cue"]),
            text=str(row.get("text", "")),
            start=float(row.get("start") or 0.0),
            end=float(row.get("end") or 0.0),
        ))
    return out


def preview(removals: list[Removal], limit: int = 12) -> str:
    """What the user reads before deciding. Cue numbers are 1-based, because
    that is what every other surface in this product shows them as."""
    if not removals:
        return "nothing to remove"
    lines = [
        f"  cue {r.cue + 1:>4}  {_clock(r.start)}  {r.text.strip()}"
        for r in removals[:limit]
    ]
    if len(removals) > limit:
        lines.append(f"  ... and {len(removals) - limit} more")
    return "\n".join(lines)


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


_SUFFIX = re.compile(r"\.[A-Za-z0-9]+$")


def _stem(source: Path) -> str:
    """`talk.zh.srt` -> `talk.zh`. One suffix, exactly as `correct._stem`
    does it and for the same reason: the language tag is part of the
    identity. Both now call the one in `refine`, so a stage cannot disagree
    with another stage about what a transcript is called."""
    from mfp.refine import stem_of

    return stem_of(source)


def write_pair(
    source: Path,
    cues: list[dict],
    removals: list[Removal],
    *,
    fillers: FillerList,
    out_dir: Path | None = None,
    accepted: list[int] | None = None,
) -> dict[str, Path]:
    """The tidied transcript, its reading copy, and the record.

    The original is not touched, so a tidying the user dislikes costs them a
    file they can delete rather than the transcript they already had. Every
    name carries `.tidy`, so a folder holding `talk.zh.srt`,
    `talk.zh.corrected.srt` and `talk.zh.tidy.srt` says which is which
    without anybody opening them.

    `accepted` indexes into `removals`; None means all of them. The record
    still lists what was OFFERED, so a run where the user kept three cues
    says so -- `summary` counts what actually went, and the two numbers
    disagreeing is the point rather than a bug.

    One stage of `refine`'s pipeline, and nothing more. This module decides
    WHAT may be deleted; where the result lands, what it is called and what
    record goes with it are the same questions for every stage, so they are
    answered in one place -- which is what lets a reading copy be corrected
    AND tidied instead of one or the other.
    """
    from mfp import refine

    return refine.write(
        Path(source),
        refine.Plan(stages=(refine.STAGE_TIDY,), cues=list(cues),
                    removals=list(removals)),
        fillers=fillers,
        accept_removals=accepted,
        out_dir=out_dir,
    )
