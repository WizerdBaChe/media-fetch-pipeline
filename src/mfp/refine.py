"""Composing the analysis verbs, so one reading copy can be both corrected
and tidied.

`correct` and `tidy` were each written as the LAST step: both owned the
transformation, the record AND the file names, so neither could be anything
but terminal. Chaining them by hand worked and cost more than it looked
like. Measured 2026-09-01 on a six-cue transcript, both orders:

    order            result text        name                        corrections record
    correct→tidy     identical          talk.zh.corrected.tidy.srt  cue [1, 3]
    tidy→correct     identical          talk.zh.tidy.corrected.srt  cue [0, 1]

The text composes. Nothing else does. One piece of content acquired two
names, seven files landed where four were wanted, and -- the part that
matters -- the two records ended up in two different coordinate systems:
`[0, 1]` are the numbers of the TIDIED file and mean nothing against the
transcript the user has. Worse, `tidy.apply` proves `restore(kept, removals)`
rebuilds its input, and in a chain that input is the intermediate, so the
property「the record can rebuild the original」silently weakened to「it can
rebuild a file nobody kept」at exactly the moment two deletions composed.

Three rules follow, and they are properties of what this module writes:

**Stage order is a property of the pipeline, not of the command line.**
Index-preserving stages run before index-collapsing ones -- `correct` before
`tidy`, whichever the user asked for first, from whichever verb or checkbox.
That is not a style preference: `correct.apply` asserts the cue count and
every timestamp are unchanged, so running it first leaves BOTH records
addressed in the source transcript's own numbering. `cueSpace: "source"` in
the record says so out loud.

**One output set, one serial, one record.** The intermediate is not written:
nobody asked for it, and a derived file with no record beside it is the
thing `runs.place_set` exists to prevent (P-62).

**The proof is end to end.** Before a byte is written, the removals are put
back and the substitutions are reversed, and the result must equal the
ORIGINAL cues -- not the intermediate. Deletion is the one defect class this
project cannot see downstream (P-50, P-59), and a composition is where a
per-stage proof stops covering it.

Single-stage runs keep today's names and today's record shape exactly. There
is nothing to migrate, and a one-stage record was already complete.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mfp.errors import MfpError

__all__ = [
    "ORDER",
    "Outcome",
    "Plan",
    "RefineRefused",
    "STAGE_CORRECT",
    "STAGE_TIDY",
    "names_for",
    "offer",
    "order",
    "plan",
    "result_key",
    "run",
    "stem_of",
    "write",
]

SCHEMA_VERSION = 1

STAGE_CORRECT = "correct"
STAGE_TIDY = "tidy"

#: Canonical order. Index-preserving first, so every stage's record indexes
#: the SAME cues the user's transcript has. Reversing this is what put the
#: corrections record into the tidied file's numbering in the measurement
#: above, and a record whose numbers refer to a file the user does not have
#: is a record of nothing.
ORDER: tuple[str, ...] = (STAGE_CORRECT, STAGE_TIDY)

#: What each stage adds to a file name. A folder holding `talk.zh.srt`,
#: `talk.zh.corrected.srt` and `talk.zh.corrected.tidy.srt` says which is
#: which without anybody opening them -- and because the order is canonical,
#: `talk.zh.tidy.corrected.srt` is a name this product can no longer produce.
_INFIX = {STAGE_CORRECT: "corrected", STAGE_TIDY: "tidy"}


class RefineRefused(MfpError):
    """The written set would not have rebuilt the transcript it came from.

    Distinct from `TidyRefused`, which is one stage's own subset proof, and
    from `CorrectionRefused`, which is one stage's span check. This is the
    COMPOSITION failing: each stage was individually fine and undoing them in
    reverse did not give the original back. Nothing is written, so the
    original is intact -- which is the first thing its message says.

    422 with the rest of the「read fine, cannot be used」family: the
    transcript was read correctly and the outcome is a fact about this
    request, never a retry.
    """

    error_code = "refine_refused"
    exit_code = 3


def order(stages) -> tuple[str, ...]:
    """The stages the pipeline will actually run, in the order it runs them.

    Deduplicated and sorted into `ORDER`. Callers hand this whatever the user
    typed or ticked; nothing downstream ever sees another arrangement.
    """
    wanted = {str(stage) for stage in stages}
    unknown = wanted - set(ORDER)
    if unknown:
        raise ValueError(f"unknown stage(s): {sorted(unknown)}")
    return tuple(stage for stage in ORDER if stage in wanted)


def stem_of(source: Path) -> str:
    """`talk.zh.srt` -> `talk.zh`. One suffix, not all of them: the language
    tag is part of the identity, and `talk.corrected.srt` sitting next to
    `talk.en.srt` and `talk.zh.srt` would belong to neither."""
    source = Path(source)
    return source.name[: -len(source.suffix)] if source.suffix else source.name


def result_key(stages: tuple[str, ...]) -> str:
    """Which key in a written set holds the transcript itself.

    Single-stage runs keep the key they have always returned, because those
    keys are labels on screen -- 「corrected」 and 「tidy」 are printed by the
    CLI and mapped to Chinese by the GUI. A composition is neither, and
    calling it either would be a claim about which stage produced it.
    """
    if stages == (STAGE_CORRECT,):
        return "corrected"
    if stages == (STAGE_TIDY,):
        return "tidy"
    return "result"


def names_for(stem: str, stages: tuple[str, ...]) -> dict[str, str]:
    """The file names one run writes, keyed by what each file is for.

    The single-stage rows are today's names, unchanged and deliberately not
    unified: `talk.zh.corrections.json` is on disks that exist, and renaming
    it to make this table prettier would strand every record already written.
    """
    if stages == (STAGE_CORRECT,):
        return {
            "corrected": f"{stem}.corrected.srt",
            "reading": f"{stem}.corrected.txt",
            "record": f"{stem}.corrections.json",
            "diff": f"{stem}.corrections.diff.txt",
        }
    if stages == (STAGE_TIDY,):
        return {
            "tidy": f"{stem}.tidy.srt",
            "reading": f"{stem}.tidy.txt",
            "record": f"{stem}.tidy.json",
        }
    infix = ".".join(_INFIX[stage] for stage in stages)
    return {
        "result": f"{stem}.{infix}.srt",
        "reading": f"{stem}.{infix}.txt",
        "record": f"{stem}.{infix}.json",
        "diff": f"{stem}.{infix}.diff.txt",
    }


@dataclass(frozen=True)
class Plan:
    """What a run WOULD do, computed in canonical order and applied to nothing.

    `removals` are proposed against the corrected cues rather than against the
    original, because that is the text the user will read -- and their `cue`
    numbers are still the original's, since the stage that ran first cannot
    move one.
    """

    stages: tuple[str, ...]
    cues: list[dict] = field(default_factory=list)
    corrections: list = field(default_factory=list)
    removals: list = field(default_factory=list)
    #: The cues after correction. What `removals` was computed against, and
    #: what the tidy stage's own record describes.
    corrected: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    """What a run DID, with the record that can undo it."""

    stages: tuple[str, ...]
    cues: list[dict]
    corrections: list
    removals: list
    record: dict


def plan(
    cues: list[dict],
    *,
    stages,
    glossary=None,
    fillers=None,
    exact_only: bool = False,
) -> Plan:
    """Everything both stages would offer, in the order they would run.

    Pure: cues in, offers out. No file system, no engine -- the shape
    `correct.propose` and `tidy.propose` already have, kept so the composition
    is as testable as its parts.
    """
    from mfp import correct as corrector, tidy as tidier

    staged = order(stages)
    corrections: list = []
    if STAGE_CORRECT in staged and glossary is not None:
        corrections = corrector.propose(
            cues, glossary, allow_phonetic=not exact_only)

    corrected = corrector.apply(cues, corrections) if corrections else list(cues)

    removals: list = []
    if STAGE_TIDY in staged and fillers is not None:
        removals = tidier.propose(corrected, fillers)

    return Plan(stages=staged, cues=list(cues), corrections=corrections,
                removals=removals, corrected=corrected)


def _accepted(offers: list, accepted: list[int] | None) -> list:
    if accepted is None:
        return list(offers)
    return [offers[i] for i in accepted if 0 <= i < len(offers)]


def _inverse(proposals: list) -> list:
    """The substitutions that undo these, in the CORRECTED text's coordinates.

    `correct.apply` works right to left using the original offsets, so a span
    in the result sits at its original start plus whatever every span to its
    LEFT changed in length. Computing that shift here is what lets the inverse
    go back through `correct.apply` itself -- which then re-checks that each
    span really does read what it is about to replace. A hand-rolled reversal
    would be a second implementation of the thing being verified.
    """
    from mfp.correct import Proposal

    by_cue: dict[int, list] = {}
    for proposal in proposals:
        by_cue.setdefault(proposal.cue, []).append(proposal)

    out: list = []
    for cue, group in by_cue.items():
        shift = 0
        for proposal in sorted(group, key=lambda p: p.start):
            start = proposal.start + shift
            out.append(Proposal(
                cue=cue,
                start=start,
                end=start + len(proposal.now),
                was=proposal.now,
                now=proposal.was,
                term=proposal.term,
                tier=proposal.tier,
            ))
            shift += len(proposal.now) - len(proposal.was)
    return out


def _same(left: list[dict], right: list[dict]) -> bool:
    return len(left) == len(right) and all(
        a.get("text") == b.get("text")
        and a.get("start") == b.get("start")
        and a.get("end") == b.get("end")
        for a, b in zip(left, right)
    )


def run(
    source_name: str,
    plan_: Plan,
    *,
    glossary=None,
    fillers=None,
    accept_corrections: list[int] | None = None,
    accept_removals: list[int] | None = None,
) -> Outcome:
    """Apply the accepted stages and prove the result can be undone.

    When the plan HAS a correction stage that found something, the removals
    are RE-DERIVED against the text this run actually produced and then
    narrowed to the cues the user accepted. Both halves matter, and the
    condition is about the PLAN rather than about what was accepted: the
    plan's removals were computed against the fully corrected text, so
    un-ticking a correction leaves them describing a line that is no longer
    there -- measured, `耗` un-corrected against a removal recorded as `好`,
    and `tidy.apply` refused a change the user is entitled to make.

    With no correction stage the removals pass through untouched, so a
    tidy-only run still meets `tidy.apply`'s check that every removal reads
    what the record says it reads. Re-deriving there would have replaced a
    refusal with a silent skip, which is the wrong trade for a record written
    against a different version of the file.
    """
    from mfp import correct as corrector, tidy as tidier

    stages = plan_.stages
    cues = plan_.cues

    corrections = _accepted(plan_.corrections, accept_corrections)
    corrected = corrector.apply(cues, corrections) if corrections else list(cues)

    removals: list = []
    if STAGE_TIDY in stages and fillers is not None:
        removals = _accepted(plan_.removals, accept_removals)
        if STAGE_CORRECT in stages and plan_.corrections:
            wanted = {r.cue for r in removals}
            removals = [r for r in tidier.propose(corrected, fillers)
                        if r.cue in wanted]

    final = (tidier.apply(corrected, removals, fillers=fillers)
             if STAGE_TIDY in stages else corrected)

    if STAGE_TIDY in stages and not final:
        raise tidier.TidyRefused(
            "every cue in this transcript is on the filler list, so the "
            "tidied copy would be empty. Nothing was written"
        )

    # The end-to-end proof. Each stage already checks itself against its own
    # input; this one checks the SET against the transcript the user has,
    # which is the only claim the record actually makes.
    rebuilt = tidier.restore(final, removals) if STAGE_TIDY in stages else final
    if corrections:
        rebuilt = corrector.apply(rebuilt, _inverse(corrections))
    if not _same(rebuilt, cues):
        raise RefineRefused(
            "putting this run back did not reproduce the transcript it was "
            f"made from ({source_name}), so nothing was written. The "
            "original has not been touched"
        )

    return Outcome(
        stages=stages,
        cues=final,
        corrections=corrections,
        removals=removals,
        record=_record(
            source_name, plan_, corrections, corrected, removals, final,
            glossary=glossary, fillers=fillers,
            accept_corrections=accept_corrections,
        ),
    )


def _record(
    source_name: str,
    plan_: Plan,
    corrections: list,
    corrected: list[dict],
    removals: list,
    final: list[dict],
    *,
    glossary,
    fillers,
    accept_corrections: list[int] | None,
) -> dict:
    """The machine-readable half.

    A single-stage run gets that stage's own record verbatim -- the shape
    `correct.patch` and `tidy.record` already produce, still on disks that
    exist. A composition gets an envelope around both, because two records in
    one file need to say which numbering they share and in what order they
    were applied. Everything needed to walk back to the original is in it.
    """
    from mfp import correct as corrector, tidy as tidier

    stages = plan_.stages
    patch = (
        corrector.patch(plan_.cues, plan_.corrections, source=source_name,
                        glossary=glossary, accepted=accept_corrections)
        if STAGE_CORRECT in stages and glossary is not None else None
    )
    removed = (
        tidier.record(corrected, removals, source=source_name, fillers=fillers,
                      offered=len(plan_.removals))
        if STAGE_TIDY in stages and fillers is not None else None
    )

    if stages == (STAGE_CORRECT,) and patch is not None:
        return patch
    if stages == (STAGE_TIDY,) and removed is not None:
        return removed

    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source_name,
        "sourceSha256": corrector.fingerprint(plan_.cues),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stages": list(stages),
        # Both stage records index the SOURCE transcript's cues. True only
        # because the index-preserving stage runs first, which is why the
        # order is the pipeline's and not the caller's.
        "cueSpace": "source",
        "summary": {
            "cues": len(plan_.cues),
            "corrected": len(corrections),
            "removed": len(removals),
            "kept": len(final),
        },
        STAGE_CORRECT: patch,
        STAGE_TIDY: removed,
    }


def offer(
    source_name: str, plan_: Plan, *, glossary=None, fillers=None
) -> dict:
    """The record a run WOULD write, computed from the plan and applied to
    nothing.

    What `--json` prints before `--apply` is given. Deliberately does not go
    through `run`: a dry run must not be able to refuse, and it must not be
    able to write. The kept count is arithmetic on the plan rather than an
    application of it.
    """
    removed = {r.cue for r in plan_.removals}
    kept = [cue for index, cue in enumerate(plan_.corrected or plan_.cues)
            if index not in removed]
    return _record(source_name, plan_, plan_.corrections,
                   plan_.corrected or plan_.cues, plan_.removals, kept,
                   glossary=glossary, fillers=fillers, accept_corrections=None)


def diff(plan_: Plan, outcome: Outcome) -> str:
    """What changed, in the form a person checks line by line.

    Both halves in one file when both stages ran: a reader who has to open two
    documents to find out what happened to one transcript will open neither.
    """
    from mfp import correct as corrector, tidy as tidier

    parts: list[str] = []
    if STAGE_CORRECT in outcome.stages:
        parts.append(corrector.diff(plan_.cues, outcome.corrections))
    if STAGE_TIDY in outcome.stages and STAGE_CORRECT in outcome.stages:
        parts.append("")
        parts.append(f"--- 刪掉的語助詞（{len(outcome.removals)} 句）")
        parts.append(tidier.preview(outcome.removals, limit=len(outcome.removals)))
    return "\n".join(parts)


def out_dir_for(source: Path, out_dir: Path | None = None) -> Path:
    """Where a derived set belongs.

    The analysis the transcript is part of, not the folder the file happens to
    sit in: the source is normally inside `字幕檔/`, and writing the corrected
    copy there would bury it one level below the run it belongs to -- and put
    the `.txt` in the subtitles folder.
    """
    from mfp import runs

    if out_dir:
        return Path(out_dir)
    found = runs.run_of(source)
    return found.root if found else Path(source).parent


def write(
    source: Path,
    plan_: Plan,
    *,
    glossary=None,
    fillers=None,
    accept_corrections: list[int] | None = None,
    accept_removals: list[int] | None = None,
    out_dir: Path | None = None,
) -> dict[str, Path]:
    """Run the plan and write the one set it produces.

    The original is not among the files. That is the ruling both stages were
    built under, and it is also what makes a composition safe to be wrong
    about: a reading copy the user dislikes costs them files they can delete,
    never the transcript they already had.
    """
    from mfp import runs
    from mfp.asr import to_srt, to_text

    source = Path(source)
    outcome = run(source.name, plan_, glossary=glossary, fillers=fillers,
                  accept_corrections=accept_corrections,
                  accept_removals=accept_removals)

    target = out_dir_for(source, out_dir)
    target.mkdir(parents=True, exist_ok=True)

    names = names_for(stem_of(source), outcome.stages)
    # One serial across the whole set (`runs.place_set`): a record filed under
    # a different serial than the copy it describes is worse than the
    # overwrite it was avoiding (P-62).
    written = runs.place_set(target, names)

    written[result_key(outcome.stages)].write_text(
        to_srt(outcome.cues), encoding="utf-8")
    written["reading"].write_text(
        to_text(str(c.get("text", "")) for c in outcome.cues), encoding="utf-8")
    written["record"].write_text(
        json.dumps(outcome.record, ensure_ascii=False, indent=1),
        encoding="utf-8")
    if "diff" in written:
        written["diff"].write_text(diff(plan_, outcome) + "\n", encoding="utf-8")
    return written
