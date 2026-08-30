"""Translate a document, keeping everything about it that is not words.

A separate verb from `mfp translate`, and separate for a measured reason
rather than tidiness. `translate.translate_file` accepts a `.txt` and splits
it with `raw.splitlines()`, one line per translated unit -- correct for a
transcript, where a line IS an utterance, and wrong for prose in three ways
at once:

  * a hard-wrapped paragraph reaches the model as fragments, and NLLB is a
    sentence-level model: half a clause translates like half a clause;
  * blank lines are dropped by that filter, so paragraph structure does not
    survive the round trip;
  * a `.md` heading, list marker, table or fenced code block is prose to it,
    so `## Results` comes back translated *including the hashes*, and a code
    block comes back translated.

So the shared part is the ENGINE -- language codes, the runner, the process
contract -- and that stays in `mfp.translate`. What is different is
segmentation and structure, and that is this module.

**Nothing here lets the model touch structure.** Anything that is not words
is held out of the request entirely rather than replaced with a placeholder
token and hoped for: a masked `` `os.path` `` that comes back mangled is a
corrupted document that no assertion downstream can see, and this build
cannot measure how NLLB treats a given placeholder without the 3 GB model
in front of it. Holding structure out costs translation quality on a
sentence broken by inline code (it becomes two shorter requests) and cannot
corrupt anything. That trade is revisited when there is a corpus to measure
it on, not before -- see `translatable_runs`.

The invariants `translate_document` asserts before it writes, all of them
the same shape as the corrector's cue-count check (D-117), because deletion
is the class nothing downstream can see:

  * every block survives, in order, with its kind;
  * every verbatim block is byte-identical to its input;
  * the engine returned exactly as many sentences as it was given;
  * a block that had words still has words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mfp.errors import MfpError, UsageError
from mfp.translate import (
    TranslationOutcome,
    TranslationUnavailable,
    flores_for,
    record,
    record_name,
    refuse_missing_source,
    refuse_self_overwrite,
    runner_path,
    translate_texts,
    write_record,
)

__all__ = [
    "DOCUMENT_SUFFIXES",
    "Block",
    "DocumentOutcome",
    "blocks_of",
    "refuse_unless_document",
    "split_sentences",
    "translatable_runs",
    "translate_document",
]

#: What this verb accepts. Markdown is parsed for structure; `.txt` is read
#: as paragraphs and nothing else, because a bare `-` in a plain-text file is
#: a dash far more often than it is a list.
DOCUMENT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

#: A fenced code block's delimiter, ``` or ~~~, with any info string.
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")

#: YAML front matter, but only as the very first line of the file. A `---`
#: anywhere else is a horizontal rule and is its own one-line block.
_FRONT_MATTER = re.compile(r"^---\s*$")

#: The markup that introduces a line without being part of its sentence.
#: Order matters: a numbered list item inside a blockquote is `> 1. `, and
#: the loop strips one prefix at a time.
_PREFIXES = (
    re.compile(r"^(\s*>\s?)"),               # blockquote
    re.compile(r"^(\s*#{1,6}\s+)"),          # ATX heading
    re.compile(r"^(\s*[-*+]\s+(?:\[[ xX]\]\s+)?)"),  # bullet, task list
    re.compile(r"^(\s*\d+[.)]\s+)"),         # ordered list
)

#: Lines that are structure all the way through and carry no sentence.
_STRUCTURAL = re.compile(
    r"^\s*(?:"
    r"[-*_]\s*[-*_]\s*[-*_][-*_\s]*"   # horizontal rule
    r"|\|.*\|"                          # table row (or its separator)
    r"|<[/!a-zA-Z][^>]*>\s*"            # a bare HTML tag on its own line
    r")$"
)

#: Spans that must reach the output byte-identical. Inline code first: a URL
#: inside backticks is code, not a link.
_INLINE_VERBATIM = re.compile(
    r"(`+[^`]*?`+"                        # inline code
    r"|!?\[[^\]]*\]\([^)]*\)"             # link or image, whole
    r"|<https?://[^>\s]+>"                # autolink
    r"|https?://\S+"                      # bare URL
    r"|\{\{[^}]*\}\}"                     # a template placeholder
    r")"
)

#: Sentence end, for the two writing systems this product actually sees.
#: CJK terminators need no following space; Latin ones do, and must not fire
#: on `Fig. 3` or `e.g. this` -- hence the lower-case guard.
_SENTENCE_END = re.compile(
    r"(?<=[。！？；…])\s*"
    r"|(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9‘“(\[])"
)

#: Abbreviations that end in a period and do not end a sentence. Short and
#: deliberately incomplete: this list can only ever be wrong in the direction
#: of one over-split sentence, which reads slightly worse and loses nothing.
_ABBREVIATIONS = (
    "e.g.", "i.e.", "cf.", "vs.", "etc.", "Fig.", "Eq.", "No.", "Dr.",
    "Mr.", "Ms.", "Mrs.", "Prof.", "St.", "approx.", "al.",
)

BLOCK_PROSE = "prose"
BLOCK_VERBATIM = "verbatim"
BLOCK_BLANK = "blank"


@dataclass(frozen=True)
class Block:
    """One structural piece of a document.

    `raw` is the input text, kept whole so a verbatim block can be written
    back without reconstructing it. `prefix` is the markup that introduces a
    prose block's FIRST line -- `## `, `- `, `> ` -- and is reattached
    unchanged, because a translated heading is still a heading.
    """

    kind: str
    raw: str
    prefix: str = ""
    body: str = ""

    @property
    def translatable(self) -> bool:
        return self.kind == BLOCK_PROSE and bool(self.body.strip())


@dataclass(frozen=True)
class DocumentOutcome(TranslationOutcome):
    """What `translate_document` did, in numbers a caller can check.

    Extends the caption outcome rather than replacing it so `--json` and the
    GUI read one shape for both verbs. `blocks` and `verbatim_blocks` are
    what make the structure claim checkable after the fact: a document whose
    fenced code count changed did not survive.
    """

    blocks: int = 0
    verbatim_blocks: int = 0
    sentences: int = 0

    def counts(self) -> dict[str, int]:
        """The caption verb's numbers plus this one's structure.

        `lines` and `sentences` are the same count here and are both kept:
        one is the shape every record shares, the other is what this verb
        actually counted, and collapsing them would make a reader work out
        which by knowing the code.
        """
        return {
            **super().counts(),
            "blocks": self.blocks,
            "verbatimBlocks": self.verbatim_blocks,
            "sentences": self.sentences,
        }


def blocks_of(text: str, *, markdown: bool) -> list[Block]:
    """Cut a document into blocks, losing nothing.

    `"".join(b.raw for b in blocks_of(t, ...)) == t` for every input, which
    is the property the round trip rests on and the one the tests assert
    against real files. A parser that can silently drop a line is a parser
    that can delete a paragraph.
    """
    lines = text.splitlines(keepends=True)
    blocks: list[Block] = []
    index = 0
    total = len(lines)

    if markdown and total and _FRONT_MATTER.match(lines[0].rstrip("\n")):
        end = _closing_front_matter(lines)
        if end is not None:
            blocks.append(Block(BLOCK_VERBATIM, "".join(lines[: end + 1])))
            index = end + 1

    while index < total:
        line = lines[index]
        bare = line.rstrip("\n")

        if not bare.strip():
            blocks.append(Block(BLOCK_BLANK, line))
            index += 1
            continue

        if markdown:
            fence = _FENCE.match(bare)
            if fence:
                end = _closing_fence(lines, index, fence.group(2))
                blocks.append(Block(BLOCK_VERBATIM, "".join(lines[index : end + 1])))
                index = end + 1
                continue
            if _STRUCTURAL.match(bare) or bare.startswith("    ") or bare.startswith("\t"):
                # Indented code, a rule, a table row, a lone HTML tag. Each
                # is its own block so a table's rows cannot be merged into
                # one paragraph and re-emitted as a sentence.
                blocks.append(Block(BLOCK_VERBATIM, line))
                index += 1
                continue

        end = _paragraph_end(lines, index, markdown=markdown)
        raw = "".join(lines[index:end])
        prefix, body = _split_prefix(raw, markdown=markdown)
        blocks.append(Block(BLOCK_PROSE, raw, prefix=prefix, body=body))
        index = end

    return blocks


def _closing_front_matter(lines: list[str]) -> int | None:
    for i in range(1, len(lines)):
        if _FRONT_MATTER.match(lines[i].rstrip("\n")):
            return i
    return None  # unterminated: not front matter at all, treat as content


def _closing_fence(lines: list[str], start: int, marker: str) -> int:
    """Index of the closing fence, or the last line when there is none.

    An unterminated fence takes the rest of the file. That is what every
    Markdown renderer does, and guessing otherwise would send code to the
    translator.
    """
    for i in range(start + 1, len(lines)):
        match = _FENCE.match(lines[i].rstrip("\n"))
        if match and match.group(2)[0] == marker[0] and len(match.group(2)) >= len(marker):
            return i
    return len(lines) - 1


def _paragraph_end(lines: list[str], start: int, *, markdown: bool) -> int:
    """Where the paragraph beginning at `start` stops.

    A blank line always ends it. In Markdown so does the start of anything
    with its own prefix -- otherwise two list items become one sentence and
    the second one's bullet is translated away.
    """
    i = start + 1
    while i < len(lines):
        bare = lines[i].rstrip("\n")
        if not bare.strip():
            break
        if markdown:
            if _FENCE.match(bare) or _STRUCTURAL.match(bare):
                break
            if any(pattern.match(bare) for pattern in _PREFIXES):
                break
            if bare.startswith("    ") or bare.startswith("\t"):
                break
        i += 1
    return i


def _split_prefix(raw: str, *, markdown: bool) -> tuple[str, str]:
    """Peel the markup off the front of a paragraph.

    Only the FIRST line can carry one; a continuation line's leading spaces
    belong to the paragraph and are folded into it by `_flatten`.
    """
    if not markdown:
        return "", _flatten(raw)
    head, _, rest = raw.partition("\n")
    prefix = ""
    remaining = head
    for pattern in _PREFIXES:
        match = pattern.match(remaining)
        if match:
            prefix += match.group(1)
            remaining = remaining[match.end() :]
    body = remaining if not rest else remaining + "\n" + rest
    return prefix, _flatten(body)


def _flatten(raw: str) -> str:
    """A hard-wrapped paragraph, as the one sentence-stream it really is."""
    return re.sub(r"\s+", " ", raw).strip()


def translatable_runs(text: str) -> list[tuple[bool, str]]:
    """`[(translate_this, text), ...]`, alternating, covering `text` exactly.

    Inline code, links, autolinks and bare URLs come back with
    `translate_this` False and are copied through untouched.

    **Why splitting rather than masking.** The usual trick is to swap each
    span for a placeholder, translate the whole sentence, and put the spans
    back -- better output, because the model sees an unbroken sentence. It
    also requires the model to return every placeholder intact, which is a
    property of the MODEL, cannot be asserted from here, and fails silently
    into a corrupted document when it does not hold. Splitting costs fluency
    across an inline span and cannot lose anything. Revisit with a masking
    scheme when there is a labelled corpus and the real engine to measure
    the two against -- the swap point is this function, and nothing above it
    needs to change.
    """
    runs: list[tuple[bool, str]] = []
    position = 0
    for match in _INLINE_VERBATIM.finditer(text):
        if match.start() > position:
            runs.append((True, text[position : match.start()]))
        runs.append((False, match.group(0)))
        position = match.end()
    if position < len(text):
        runs.append((True, text[position:]))
    return runs or [(True, text)]


def split_sentences(text: str) -> list[str]:
    """One sentence per item, joinable back into `text` with a space.

    NLLB is a sentence-level model, so a paragraph handed over whole comes
    back worse than the same paragraph handed over a sentence at a time.
    This is a splitter, not a parser: getting it wrong costs one clumsy
    sentence boundary and never a lost word, which is why the abbreviation
    list is allowed to be short.
    """
    stripped = text.strip()
    if not stripped:
        return []
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(stripped):
        end = match.start() if match.group().strip() == "" else match.end()
        candidate = stripped[start:end].strip()
        if not candidate:
            continue
        if any(candidate.endswith(abbrev) for abbrev in _ABBREVIATIONS):
            continue
        pieces.append(candidate)
        start = match.end()
    tail = stripped[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces or [stripped]


def refuse_unless_document(source: Path) -> Path:
    """The two refusals that must come before anything is CREATED.

    `translate_document` calls this itself, so no caller can skip it. Callers
    that resolve an output folder first call it BEFORE they do, because
    `runs.open_run` only ever creates: a refusal that has already made an
    empty analysis folder for a file it then declines to read is a refusal
    that littered (P-64).

    Suffix first and existence second, deliberately. A `.pdf` that is not
    there is both, and "this verb does not take a .pdf" is the more useful
    of the two facts -- the other one is fixable by finding the file, and
    that would not help.
    """
    if source.suffix.lower() not in DOCUMENT_SUFFIXES:
        raise UsageError(
            f"{source.name} is not a document this can translate. It takes "
            f"{', '.join(sorted(DOCUMENT_SUFFIXES))} -- for a caption file "
            f"with timings, use `mfp translate`, which keeps them"
        )
    return refuse_missing_source(source)


def translate_document(
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
) -> DocumentOutcome:
    """Translate one document and leave a document of the same shape beside it.

    `source_language` is required in practice: a document has no filename
    convention this project wrote, so there is nothing to read it off. The
    refusal says so rather than guessing, because a wrong source language
    produces fluent output that is not a translation of anything.
    """
    from mfp import runs

    say = say or (lambda _m: None)

    refuse_unless_document(source)
    suffix = source.suffix.lower()

    source_flores = flores_for(source_language)
    if source_flores is None:
        raise UsageError(
            "name the document's language with --from, using an ISO code "
            "like `en` or a FLORES code like `eng_Latn`. Unlike a transcript "
            "this project wrote, a document carries no language in its name"
        )
    target_flores = flores_for(target)
    if target_flores is None:
        raise UsageError(
            f"{target!r} is not a language this understands. Use an ISO code "
            f"like `en` or a FLORES code like `eng_Latn`"
        )
    if target_flores == source_flores:
        raise UsageError(
            f"the document is already in {source_flores}; translating it to "
            f"itself would just cost time"
        )

    raw = source.read_text(encoding="utf-8", errors="replace")
    blocks = blocks_of(raw, markdown=suffix in MARKDOWN_SUFFIXES)
    if "".join(block.raw for block in blocks) != raw:
        # Structural, not defensive: everything below assumes the parse is
        # lossless, and a parser that quietly drops a line would delete a
        # paragraph with every assertion still green.
        raise MfpError(
            "the document parser did not account for every byte of "
            f"{source.name}; refusing to write a translation from it"
        )

    plan = _plan(blocks)
    if not plan.sentences:
        raise UsageError(f"there is nothing to translate in {source.name}")

    runner = runner_path()
    if not runner.is_file():
        raise MfpError(f"the packaged translation runner is missing at {runner}")

    say(
        f"translating {len(plan.sentences)} sentences in "
        f"{plan.prose_blocks} blocks: {source_flores} -> {target_flores}"
    )
    translated, payload, suspect, splits = translate_texts(
        runner,
        texts=plan.sentences,
        target_flores=target_flores,
        python_exe=python_exe,
        model_dir=model_dir,
        source_flores=source_flores,
        device=device,
        compute_type=compute_type,
        say=say,
        on_progress=on_progress,
    )
    if len(translated) != len(plan.sentences):
        raise MfpError(
            f"the translator returned {len(translated)} sentences for "
            f"{len(plan.sentences)} -- refusing to write a document whose "
            f"paragraphs do not line up"
        )
    if suspect:
        say(
            f"{len(suspect)} sentence(s) may have lost a clause -- the engine "
            f"ended them mid-sentence. Compare against the original before "
            f"relying on them"
        )

    rendered = _render(blocks, plan, translated)
    _assert_structure_survived(blocks, rendered, markdown=suffix in MARKDOWN_SUFFIXES)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.stem
    name = f"{stem}.{target_flores}{suffix}"
    refuse_self_overwrite(source, runs.place(out_dir, name))
    # The document and its record take ONE serial between them, for the
    # reason `tidy` writes its three that way: a record filed under a
    # different serial than the file it describes is worse than the overwrite
    # it was avoiding (P-62).
    written = runs.place_set(
        out_dir, {"translation": name, "record": record_name(stem, target_flores)}
    )
    target_path = written["translation"]
    target_path.write_text(rendered, encoding="utf-8")
    say(f"saved: {target_path}")

    outcome = DocumentOutcome(
        suspect_lines=tuple(suspect),
        source=target_path,
        target_language=target_flores,
        source_language=source_flores,
        line_count=len(translated),
        engine=payload.get("engine") or {},
        clause_splits=splits,
        record=written["record"],
        blocks=len(blocks),
        verbatim_blocks=sum(1 for b in blocks if b.kind == BLOCK_VERBATIM),
        sentences=len(plan.sentences),
    )
    write_record(written["record"], record(outcome, source=source))
    return outcome


@dataclass
class _Plan:
    """Which sentence belongs to which run of which block.

    A flat list goes to the engine -- one subprocess, one batch -- and this
    is what puts the answers back where they came from.
    """

    sentences: list[str]
    #: `[(block_index, [(translate, text_or_slot), ...]), ...]` where a slot
    #: is an index into `sentences`.
    layout: list[tuple[int, list[tuple[bool, object]]]]
    prose_blocks: int


def _plan(blocks: list[Block]) -> _Plan:
    sentences: list[str] = []
    layout: list[tuple[int, list[tuple[bool, object]]]] = []
    prose = 0
    for index, block in enumerate(blocks):
        if not block.translatable:
            continue
        prose += 1
        pieces: list[tuple[bool, object]] = []
        for is_prose, run in translatable_runs(block.body):
            if not is_prose or not run.strip():
                # Held back, or the whitespace BETWEEN two held-back spans.
                pieces.append((False, run))
                continue
            # `split_sentences` strips, so a run's own edge whitespace has to
            # be carried separately -- otherwise `[link](url) too.` renders
            # as `[link](url)too.` and the document loses a space every time
            # a sentence touches an inline span.
            lead = run[: len(run) - len(run.lstrip())]
            trail = run[len(run.rstrip()) :]
            if lead:
                pieces.append((False, lead))
            for sentence in split_sentences(run):
                pieces.append((True, len(sentences)))
                sentences.append(sentence)
            if trail:
                pieces.append((False, trail))
        layout.append((index, pieces))
    return _Plan(sentences=sentences, layout=layout, prose_blocks=prose)


def _render(blocks: list[Block], plan: _Plan, translated: list[str]) -> str:
    """Put the translated sentences back into the document's own shape."""
    replacements: dict[int, str] = {}
    for block_index, pieces in plan.layout:
        parts: list[str] = []
        for is_slot, value in pieces:
            parts.append(translated[value] if is_slot else str(value))
        block = blocks[block_index]
        # One space between sentences, none around a verbatim span that
        # already carries its own -- the runs cover the source exactly, so
        # joining them back is concatenation with sentence spacing only.
        body = _join(pieces, parts)
        trailing = "\n" if block.raw.endswith("\n") else ""
        replacements[block_index] = f"{block.prefix}{body}{trailing}"

    return "".join(
        replacements.get(index, block.raw) for index, block in enumerate(blocks)
    )


def _join(pieces: list[tuple[bool, object]], parts: list[str]) -> str:
    out: list[str] = []
    for position, ((is_slot, _), text) in enumerate(zip(pieces, parts)):
        if position and is_slot and pieces[position - 1][0]:
            out.append(" ")   # two sentences, one space
        out.append(text)
    return "".join(out).strip()


def _assert_structure_survived(
    blocks: list[Block], rendered: str, *, markdown: bool
) -> None:
    """Refuse to write a document that lost a piece of itself.

    Deletion is the class nothing downstream can see (P-59, D-117): a
    translation missing a code block reads perfectly and is wrong. So the
    output is re-parsed with the same parser and compared to the input --
    every block still there, in order, with its kind, and every verbatim
    block byte-identical.
    """
    after = blocks_of(rendered, markdown=markdown)
    if len(after) != len(blocks):
        raise MfpError(
            f"the translated document has {len(after)} blocks where the "
            f"original had {len(blocks)}; refusing to write it"
        )
    for index, (before, now) in enumerate(zip(blocks, after)):
        if before.kind != now.kind:
            raise MfpError(
                f"block {index} changed from {before.kind} to {now.kind} in "
                f"translation; refusing to write it"
            )
        if before.kind == BLOCK_VERBATIM and before.raw != now.raw:
            raise MfpError(
                f"block {index} is meant to be copied verbatim and did not "
                f"survive intact; refusing to write it"
            )
        if before.translatable and not now.body.strip():
            raise MfpError(
                f"block {index} had words and the translation has none; "
                f"refusing to write it"
            )


# Re-exported so a caller needs one import for both failure modes.
__all__.append("TranslationUnavailable")
