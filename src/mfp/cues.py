"""Cues and timecodes -- the text half of a subtitle, with no tools in it.

Split out of `stack.py` (2026-08-30) because four modules were importing it
and only two of them stack anything. `transcript` reads cue lines out of a
caption file; `translate` needed the timestamp regex so badly it reached for
three of this module's private names across a module boundary; the frame
stacker is one consumer of the same parser, not its owner.

**Nothing here runs a program or touches a network.** That is the property
worth keeping: every function is a pure transformation of text a caller
already has, so a test of cue arithmetic costs no ffmpeg, no yt-dlp and no
temp file. `quality.py` earns its trust the same way (D-109), and the same
kind of import probe would see this one broken.

The parser both readers share is `caption_cues`, and which cues survive is
decided there and nowhere else -- a caller that indexes the result (the
corrector says 「第 N 句」) is counting the same cues `translate.parse_cues`
counts. P-59 is what happens when two readers disagree about how many
things there are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from mfp.errors import UsageError

__all__ = [
    "Cue",
    "TAG",
    "TIMESTAMP",
    "ROLLING_SHARE",
    "caption_cues",
    "cue_lines",
    "group_lines",
    "in_window",
    "is_rolling",
    "parse_timecode",
    "parse_transcript",
    "read_cue_lines",
    "seconds_of",
    "split_block",
    "unique_lines",
]


#: An SRT or WebVTT timestamp. One regex for both: the only difference
#: that matters is `,` versus `.` before the milliseconds.
#:
#: Public because `translate.parse_cues` needs exactly this and used to
#: import it as `stack._TS` -- reaching across a module boundary for a
#: private name, which is the shape that says a module is in the wrong file.
TIMESTAMP = re.compile(r"(\d\d):(\d\d):(\d\d)[,.](\d\d\d)")

#: Inline caption markup (`<c.colorE5E5E5>`, `<i>`), stripped before the
#: words are counted or shown. Public for the same reason as `TIMESTAMP`.
TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Cue:
    """One strip's worth of subtitle: when it is on screen, and its text.

    `text` is empty on the hard-subtitle path -- there the pixels already
    carry the words and nothing needs to know what they say.
    """

    start: float
    end: float
    text: str = ""


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def parse_timecode(raw: str) -> float:
    """`90`, `1:30`, `1:30.5`, `00:01:30.500` -> seconds."""
    raw = raw.strip()
    if not raw:
        raise UsageError("empty timecode")
    parts = raw.split(":")
    if len(parts) > 3:
        raise UsageError(f"not a timecode: {raw!r}")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise UsageError(f"not a timecode: {raw!r}") from None
    total = 0.0
    for v in vals:
        total = total * 60 + v
    if total < 0:
        raise UsageError(f"negative timecode: {raw!r}")
    return total


# ---------------------------------------------------------------------------
# Soft cues -- a caption file
# ---------------------------------------------------------------------------
def caption_cues(text: str) -> list[tuple[float, float, list[str]]]:
    """`(start, end, [display line, ...])` for every cue, in file order.

    The parser both readers share. Which cues survive is decided HERE and
    nowhere else, so a caller that indexes the result counts the same cues
    every other reader of this file would. A cue whose body is empty after
    markup is stripped is not a cue.

    Handles both SRT and WebVTT; the only difference that matters is the
    timestamp separator, and `TIMESTAMP` accepts either.
    """
    out: list[tuple[float, float, list[str]]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln.rstrip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        i = 1 if lines[0].strip().isdigit() else 0
        stamps = list(TIMESTAMP.finditer(lines[i]))
        if len(stamps) != 2:
            continue
        body = [
            re.sub(r"\s+", " ", TAG.sub("", raw)).strip() for raw in lines[i + 1:]
        ]
        body = [line for line in body if line]
        if not body:
            continue
        out.append((seconds_of(stamps[0]), seconds_of(stamps[1]), body))
    return out


#: How much of a file has to repeat itself before it is treated as ROLLING.
#: A rolling file repeats on nearly every cue -- the fixture is 3 of its 3
#: adjacent pairs, and a measured 50 s YouTube window is 46 of 46. A file of
#: ordinary cues that happens to say 「嗯」 twice in a row scores a few
#: percent. A third is far above the second and far below the first.
ROLLING_SHARE = 0.34


def is_rolling(cues: list[tuple[float, float, list[str]]]) -> bool:
    """Does this file repeat the previous cue's line on the next cue?

    YouTube's automatic captions roll two lines at a time: every display cue
    repeats the previous line and appends the next, with 0.01 s transition
    cues between. Measured on one 50 s window: 47 cues carrying 25 distinct
    lines. Treating a cue as a unit there stacks near-duplicates.

    It is a property of the FILE, measured once, and that is the whole point.
    The rule used to be applied to every file unconditionally and by whole-file
    identity, which silently deleted every repetition a speaker ever made:
    「嗯」 said at 3:12 and again at 7:45 became one line, and the deletion was
    invisible in the one place a reader would check it -- the transcript on
    screen, which is what `mfp.quality` cannot see either (P-51).
    """
    if len(cues) < 2:
        return False
    repeats = sum(
        1 for before, after in zip(cues, cues[1:]) if set(before[2]) & set(after[2])
    )
    return repeats / (len(cues) - 1) >= ROLLING_SHARE


def in_window(cue: tuple[float, float, list[str]], start: float, end: float) -> bool:
    """A cue that overlaps the window at all is IN it.

    Half a cue is not a thing: a cue straddling the edge contributes all of
    its lines, or the strip would show a line the viewer never saw complete.
    """
    return not (cue[1] <= start or cue[0] >= end)


def cue_lines(text: str, start: float, end: float) -> list[tuple[float, str]]:
    """One entry per cue, whole and in order. Nothing is dropped.

    The faithful reader, for every caption file that is not rolling: a
    recognised transcript, a written track, a `.srt` somebody handed us. Its
    entries line up one-for-one with `translate.parse_cues`, which is what
    lets 「第 N 句」 in a correction name the same sentence the reader is
    looking at.
    """
    return [
        (cue[0], " ".join(cue[2]))
        for cue in caption_cues(text)
        if in_window(cue, start, end)
    ]


def unique_lines(text: str, start: float, end: float) -> list[tuple[float, str]]:
    """Distinct caption LINES in the window, keyed by first appearance.

    The ROLLING reader. Only reach for it on a file `is_rolling` says is one:
    de-duplicating by whole-file identity is content deletion everywhere else,
    and content deletion is the class nothing downstream can see.
    """
    seen: set[str] = set()
    out: list[tuple[float, str]] = []
    for cue in caption_cues(text):
        if not in_window(cue, start, end):
            continue
        for body in cue[2]:
            if body not in seen:
                seen.add(body)
                out.append((cue[0], body))
    return out


def seconds_of(m: re.Match) -> float:
    h, mi, s, ms = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


_TRANSCRIPT_TS = re.compile(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*$")


def parse_transcript(text: str, start: float, end: float, chars: int = 70
                     ) -> list[tuple[float, str]]:
    """A plain transcript: a timestamp alone on a line, then what was said.

    This is what YouTube's "show transcript" copies out, and what sits beside
    a downloaded video as a `.txt`. It carries no end times -- the next stamp
    is the end -- and its blocks are paragraphs rather than display lines,
    because nothing here was ever meant to fit on screen.

    That last part matters downstream: a paragraph is already a whole strip's
    worth, so it must not be grouped with its neighbours the way caption
    lines are.
    """
    blocks: list[tuple[float, list[str]]] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        stamp = _TRANSCRIPT_TS.match(raw)
        if stamp:
            blocks.append((parse_timecode(stamp.group(1)), []))
        elif blocks and raw.strip():
            blocks[-1][1].append(raw.strip())
    out: list[tuple[float, str]] = []
    for i, (at, body) in enumerate(blocks):
        stop = blocks[i + 1][0] if i + 1 < len(blocks) else at + 5.0
        if stop <= start or at >= end:
            continue
        joined = re.sub(r"\s+", " ", " ".join(body)).strip()
        if not joined:
            continue
        # Filter AFTER splitting, not before. A block is about eight seconds
        # long, so a block that merely overlaps the window would otherwise
        # drag its whole eight seconds of text in -- asking for 0:00-0:01 of
        # this sample returned three strips covering the first eight seconds.
        # Once the block is split, the piece is the unit the window applies to.
        pieces = split_block(at, stop, joined, chars)
        for i, (piece_at, piece) in enumerate(pieces):
            piece_end = pieces[i + 1][0] if i + 1 < len(pieces) else stop
            if piece_end > start and piece_at < end:
                out.append((piece_at, piece))
    return out


def split_block(at: float, stop: float, text: str, chars: int
                ) -> list[tuple[float, str]]:
    """Cut one transcript paragraph into strip-sized pieces.

    A block is about eight seconds of speech. Rendered whole it takes six or
    seven rows and buries the picture behind it, which is the opposite of
    what a stacked quote is for.

    The timestamps this produces are INTERPOLATED: only the block start is
    real, and each piece is placed by its share of the block's characters.
    Speech is not evenly paced, so a piece can sit a second or two off the
    frame where those exact words were said. That is a deliberate trade --
    a transcript records no finer timing, and the alternative is one
    unreadable strip per paragraph.
    """
    words = text.split()
    if not words:
        return []
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        if current and len(" ".join(current)) + 1 + len(word) > chars:
            pieces.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        pieces.append(" ".join(current))

    span = max(0.0, stop - at)
    total = sum(len(p) for p in pieces) or 1
    out: list[tuple[float, str]] = []
    seen = 0
    for piece in pieces:
        out.append((at + span * seen / total, piece))
        seen += len(piece)
    return out


def read_cue_lines(text: str, start: float, end: float, chars: int = 70
                   ) -> tuple[list[tuple[float, str]], bool]:
    """`(lines, is_transcript)`, sniffing which of the two shapes this is.

    `-->` is the one token both SRT and WebVTT must carry and a transcript
    never does, so it decides without guessing at file extensions.

    A caption file is then read one of two ways, and the FILE picks which.
    Rolling captions have to be collapsed or every strip is a near-duplicate
    of the last; everything else has to be read whole or the reader is shown
    a transcript with the speaker's repetitions quietly removed. One rule for
    both was the defect: 「嗯」「好」 came out of the engine, went into the
    `.srt` and the `.txt` on disk, and never reached the screen.
    """
    if "-->" in text:
        cues = caption_cues(text)
        if is_rolling(cues):
            return unique_lines(text, start, end), False
        return cue_lines(text, start, end), False
    return parse_transcript(text, start, end, chars), True


def group_lines(lines: Sequence[tuple[float, str]], per: int) -> list[Cue]:
    """Re-flow caption lines into `per`-line strips.

    The source's own line breaking is not binding on this path: the text is
    re-rendered, so it can be regrouped into whatever reads best. (The
    hard-subtitle path has no such freedom -- there the breaks are already
    baked into the pixels.)
    """
    if per < 1:
        raise UsageError("lines_per_strip must be >= 1")
    cues: list[Cue] = []
    for i in range(0, len(lines), per):
        chunk = list(lines[i:i + per])
        nxt = lines[i + per][0] if i + per < len(lines) else chunk[-1][0] + 2.5
        cues.append(Cue(chunk[0][0], nxt, "\n".join(t for _, t in chunk)))
    return cues
