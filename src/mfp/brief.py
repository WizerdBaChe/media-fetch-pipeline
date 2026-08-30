"""`mfp brief` -- hand an agent a post's pictures and a place to write.

**This module runs no model and makes no judgement about a picture** (D-88).
The caller is already an agent with vision, and it is the best analyser in the
room. What was missing was never a capability: it was an EXIT and a
DESTINATION, exactly the shape `transcript` has. So `brief` fetches, describes
what it fetched, names the file the explanation belongs in -- and stops.

Three things here are load-bearing and easy to undo by accident:

1. **One fetch, one rung, both purposes** (D-89). The file the agent looks at
   IS the file that is kept. There is no second copy at a second quality, so
   the rung in `BriefConfig.policy` is the only quality decision the post ever
   gets. Measurement is why: Instagram's 1080 ceiling is already under
   Claude's downscale threshold, so a smaller analysis copy would have saved
   about US$0.027 a post and cost a whole second-file mechanism.

2. **The author's words are `untrusted`** (INV-B6). A caption is written by
   someone who has never been authenticated, and this is the first thing in
   this tool that hands such text to an agent holding tools. It travels in a
   named block so the SHAPE carries the warning -- a reader cannot reach the
   caption without passing through the word.

3. **Appending, never rewriting** (INV-B4). The analysis file is a substrate
   for later work, not a document. Something already summarised cannot be
   re-summarised in a new direction, so nothing here ever edits an entry that
   is already on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mfp.errors import MfpError
from mfp.models import (
    BriefExisting,
    BriefImage,
    BriefPackage,
    BriefPost,
    BriefSkipped,
    BriefUntrusted,
    FetchResultBudget,
    Manifest,
)

#: `_analysis.content.md` / `_analysis.visual.md`, beside the media.
ANALYSIS_STEM = "_analysis"

#: Line 1 of an analysis file. Identifies it and pins the format a later
#: reader parses, so a file found on disk years from now says what it is.
ANALYSIS_HEADER = "<!-- mfp-brief lane={lane} schemaVersion=1 -->"

#: The invisible half of an entry heading. Renders as nothing; exists so the
#: heading cannot be forged by the body above it.
ENTRY_MARKER = "<!-- mfp-entry -->"

#: An entry opens with `## <ISO-8601> <!-- mfp-entry -->` at column 0.
#:
#: Two guards, and both are needed. The body is the agent's prose written
#: VERBATIM, so it contains `## headings` of its own: a pattern of
#: `^## (\S+)$` reads every one as a new entry, silently splitting one
#: explanation into several and truncating each. Requiring the timestamp
#: shape fixes the ordinary case and not the adversarial one -- a post about
#: a changelog could carry `## 2026-08-25T14:03:11` in its explanation, and
#: the split would be silent. The marker is what makes the heading
#: unforgeable in practice while staying invisible when the file is read.
_ENTRY_RE = re.compile(
    rf"^## (\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}\S*) {re.escape(ENTRY_MARKER)}\s*$",
    re.MULTILINE,
)

LANES = ("content", "visual")


class AnalysisWriteFailed(MfpError):
    """The explanation could not be written beside the media.

    Its own code because the failure is not the fetch's: the pictures are on
    disk and fine, and what the user has to fix is a directory, a permission
    or a full disk. Reporting it as a fetch failure would send them to
    re-download files they already have.

    The attribute is `error_code`, not `code`. It was `code` from 2026-08-25
    until 2026-08-27, which left `MfpError.error_code` at its `unknown_error`
    default -- so the one command whose whole contract is "every failure
    carries a code" reported the code that means "we have no idea", and named
    the error bundle after it. Three registries that exist to catch exactly
    this could not: `all_wire_error_codes()` reads `error_code` off
    `__dict__` and found nothing, `test_error_registry.py` did not import
    this module, and `test_brief.py` asserted `.code` -- so the test mirrored
    the typo and stayed green. Listed in `CLI_ONLY_ERROR_CODES` because
    `brief` has no HTTP route; see that constant for the membership rule.
    """

    error_code = "analysis_write_failed"
    exit_code = 1


def _one_line(value: str) -> str:
    """A metadata value, flattened to the one line it is written on.

    `- key: value` is a one-line format, and every value in it comes from
    somewhere outside this module: `question` from an agent that has just
    read an untrusted caption, `post_id` from `manifest.source.id`, which is
    an unconstrained `str` the platform fills in. A newline inside one of
    them ends the metadata block early, and everything after it is read back
    as part of the agent's explanation -- so a stranger's text arrives inside
    the analysis with no boundary marking it as theirs.

    Flattening rather than rejecting: this is metadata about somebody's
    photos, and refusing to record an explanation because a caption had a
    line break would be a worse failure than a space where a newline was.
    `append_entry`'s heading defusing then runs over the assembled entry as
    the second layer -- this one keeps the SHAPE, that one keeps a heading
    unforgeable.
    """
    return " ".join(value.split())


def analysis_path(post_dir: Path | str, lane: str) -> Path:
    """Where this lane's explanations live. One file per lane."""
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}")
    return Path(post_dir) / f"{ANALYSIS_STEM}.{lane}.md"


@dataclass(frozen=True)
class AnalysisEntry:
    """One `## <timestamp>` block, as a later reader sees it."""

    written_at: str
    body: str
    #: The `- key: value` lines, without their bullet. Kept as written rather
    #: than parsed into fields: this file outlives the schema that wrote it,
    #: and a reader that insists on today's keys cannot read yesterday's file.
    metadata: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", tuple(self.metadata))


def read_entries(path: Path) -> list[AnalysisEntry]:
    """Parse an analysis file into its entries, newest LAST.

    Deliberately forgiving: a file a human has edited by hand is still worth
    counting. Anything before the first `## ` is preamble and is ignored, and
    a body is returned verbatim -- this never interprets the agent's prose.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    marks = list(_ENTRY_RE.finditer(text))
    entries: list[AnalysisEntry] = []
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        metadata, body = _split_entry(text[mark.end() : end])
        entries.append(
            AnalysisEntry(written_at=mark.group(1), metadata=metadata, body=body)
        )
    return entries


def _split_entry(block: str) -> tuple[list[str], str]:
    """`(metadata lines, body)` for the text after one `## <timestamp>`.

    The metadata is the FIRST contiguous run of `- ` lines; the body is
    everything after the blank line that ends it, verbatim. The blank line is
    what makes this unambiguous when the body itself opens with a list -- and
    a body that opens with a list is ordinary prose, not an edge case.
    """
    lines = block.split("\n")
    cursor = 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    metadata: list[str] = []
    while cursor < len(lines) and lines[cursor].startswith("- "):
        metadata.append(lines[cursor][2:])
        cursor += 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    return metadata, "\n".join(lines[cursor:]).strip("\n")


def describe_existing(path: Path) -> BriefExisting | None:
    """What is already written in this lane, or None when nothing is."""
    entries = read_entries(path)
    if not entries:
        return None
    return BriefExisting(
        path=str(path), written_at=entries[-1].written_at, entries=len(entries)
    )


def append_entry(
    path: Path,
    *,
    lane: str,
    body: str,
    post_id: str,
    images: int,
    size: str | None = None,
    question: str | None = None,
    now: datetime | None = None,
) -> AnalysisEntry:
    """Append one entry. Never rewrites an existing one (INV-B4).

    The header line is written only when the file is new, so re-running this
    cannot produce a file with two headers. Written as one `open(..., "a")`
    with `newline="\\n"`: the file is UTF-8 with LF endings on every platform,
    because a mixed-ending file is what an append onto a CRLF file produces.

    Heading defusing happens ONCE, on the assembled entry, and that placement
    is the point. It used to run on `body` alone, because `body` was the field
    the 2026-08-25 security review was looking at -- but three caller-supplied
    strings land in this entry at the same parse level, and the other two
    (`question`, `post_id`) went in raw. A newline plus a well-formed heading
    in either one produced an entry nobody wrote and truncated the real one.
    Defusing what actually gets written covers every field by construction,
    including whichever field somebody adds next.
    """
    moment = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    lines = [
        f"## {moment} {ENTRY_MARKER}",
        "",
        f"- images: {images}" + (f" ({_one_line(size)})" if size else ""),
    ]
    lines.append(f"- postId: {_one_line(post_id)}")
    if question:
        lines.append(f"- question: {_one_line(question)}")
    lines += ["", body.strip("\n"), ""]

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        # The entry's OWN heading is line 0 of `lines` and must survive, so
        # the defuser runs on everything after it and the heading is put back
        # unchanged. Defusing the whole chunk would neuter the very line the
        # marker exists to make readable.
        chunk = lines[0] + "\n" + _defuse_entry_headings("\n".join(lines[1:])) + "\n"
        if new_file:
            chunk = ANALYSIS_HEADER.format(lane=lane) + "\n\n" + chunk
        # ONE write, not a header write followed by a body write. Two
        # invocations appending to the same lane at once (two agent sessions,
        # a script run twice) can interleave between calls, and the result is
        # an entry split across another entry's body -- wrong counts and wrong
        # content for a later reader, with no error raised.
        #
        # This narrows the window rather than closing it: a single buffered
        # write is not a guaranteed atomic append on Windows, and a real fix
        # is an OS advisory lock. Not taken, because this is a single-user
        # local tool where two simultaneous writers to one post's lane is not
        # a situation that arises -- stated here so that the day it does, the
        # answer is already written down.
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(chunk)
    except OSError as exc:
        failure = AnalysisWriteFailed(
            f"could not write the explanation to {path}: {exc}. The media is "
            "already saved -- this is a problem with the directory, not the "
            "fetch. Your explanation is in the error bundle as `body.md`.",
            path=str(path),
        )
        # The body exists nowhere else at this moment. `brief-save` reads it
        # from stdin and nothing else (PowerShell 5.1 silently truncates an
        # argument containing `"` or `[`, so there is no `--body` flag), so
        # a failed append destroys the one expensive thing in the run -- and
        # it fails on a full disk or a wrong directory, which is exactly when
        # retrying produces the same failure again. `log_files` rather than
        # `**context` follows `stack._with_evidence`'s rule: context is
        # serialised into `errorDetail`, and a whole essay does not belong on
        # a wire format.
        failure.log_files = {"body.md": body}
        raise failure from exc

    return AnalysisEntry(written_at=moment, body=body)


# --- assembling the package --------------------------------------------------


#: Zero width, renders as nothing, and is enough to stop `_ENTRY_RE`.
_ZERO_WIDTH = "​"


def _defuse_entry_headings(text: str) -> str:
    """Break any line in `text` that would parse as an entry heading.

    The ONE exception to writing an entry verbatim, and it is narrow: only a
    line matching `_ENTRY_RE` exactly is touched, and the only change is a
    zero-width space inside the marker, so what a human reads is unchanged.

    Why it is needed at all: `SKILL.md` tells the agent that a suspicious
    caption should be QUOTED to the user, and B-3 says the saved body is the
    detailed version of that same explanation. So a caption engineered to
    contain a full entry heading has a designed path into this file -- not an
    accident, a route the contract itself opens. Left alone it would split one
    explanation into two and truncate both, silently, at the next read.

    Found by a security review, 2026-08-25. The earlier adversarial test used
    a timestamp WITHOUT the marker and the marker WITHOUT a timestamp, and
    `_ENTRY_RE` needs both -- so the shape that actually matters was the one
    shape not covered.

    Takes `text`, not `body`, since 2026-08-27: the health check found it was
    guarding one of the three caller-supplied strings that reach an entry.
    `append_entry` now calls it on the assembled entry, so the parameter name
    is the honest one and no future field can be added past it.
    """
    if ENTRY_MARKER not in text:
        return text
    defused = ENTRY_MARKER.replace("<!--", "<!--" + _ZERO_WIDTH, 1)
    return "\n".join(
        line.replace(ENTRY_MARKER, defused) if _ENTRY_RE.match(line) else line
        for line in text.split("\n")
    )


def _skip_reason(kind: str) -> tuple[str, str]:
    """(`BriefSkipped.kind`, wire reason) for an item that is not an image."""
    if kind == "video":
        return "video", "video_not_supported_yet"
    return "other", "unsupported_item_kind"


def file_size(path: Path) -> tuple[int, int] | None:
    """`(width, height)` of a file already on disk. Local, free, no network.

    The package reports what the FILE is, not what the platform claimed --
    which is also what makes the reused path and the freshly-fetched path
    describe an image identically. Pillow is imported here rather than at
    module scope so a broken install costs an unknown size, not the command.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except (OSError, ValueError, ModuleNotFoundError):
        return None
    return (width, height) if width and height else None


def partition_items(
    manifest: Manifest, files: dict[int, Path]
) -> tuple[list[BriefImage], list[BriefSkipped]]:
    """Split every manifest item into `images` or `skipped` -- never neither.

    INV-B3. A post whose video half vanished silently reads as a post that
    never had one, so an item this build cannot hand over is REPORTED rather
    than dropped. `files` maps item index to the file that landed; an image
    with no entry there failed to transfer and is skipped with that reason
    rather than pointing at a path with nothing behind it.
    """
    images: list[BriefImage] = []
    skipped: list[BriefSkipped] = []

    for item in manifest.items:
        if item.kind != "image":
            kind, reason = _skip_reason(item.kind)
            skipped.append(BriefSkipped(index=item.index, kind=kind, reason=reason))
            continue

        path = files.get(item.index)
        if path is None or not path.exists():
            skipped.append(
                BriefSkipped(index=item.index, kind="other", reason="transfer_failed")
            )
            continue

        size = file_size(path)
        images.append(
            BriefImage(
                index=item.index,
                path=str(path.resolve()),
                width=size[0] if size else None,
                height=size[1] if size else None,
                bytes=path.stat().st_size,
            )
        )

    return images, skipped


def untrusted_block(manifest: Manifest) -> BriefUntrusted:
    """Everything the post's AUTHOR wrote, gathered in one named place.

    INV-B6. Nothing that comes out of here is an instruction, however it is
    phrased -- it is a string a stranger typed into a public form.
    """
    return BriefUntrusted(
        caption=manifest.source.caption,
        alt_text={str(item.index): item.alt_text for item in manifest.items},
    )


def build_package(
    manifest: Manifest,
    *,
    lane: str,
    post_dir: Path,
    files: dict[int, Path],
    budget: FetchResultBudget,
    reused: bool,
    degraded_reason: str | None = None,
) -> BriefPackage:
    """Assemble the object `mfp brief --json` prints.

    Pure: everything that touches the network or the clock has happened by
    the time this runs, which is what makes the shape testable without one.
    """
    images, skipped = partition_items(manifest, files)
    path = analysis_path(post_dir, lane)

    unresolved = [image for image in images if image.width is None or image.height is None]
    reason = degraded_reason
    if reason is None and unresolved:
        reason = "image_size_unresolved"

    source = manifest.source
    return BriefPackage(
        lane=lane,  # type: ignore[arg-type]
        post=BriefPost(
            platform=source.platform,
            url=source.url,
            id=source.id,
            author=source.author,
            timestamp=source.timestamp,
            post_dir=str(post_dir),
        ),
        untrusted=untrusted_block(manifest),
        images=images,
        skipped=skipped,
        analysis_path=str(path),
        existing=describe_existing(path),
        budget=budget,
        reused=reused,
        degraded=reason is not None,
        degraded_reason=reason,
    )


__all__ = [
    "ANALYSIS_HEADER",
    "ANALYSIS_STEM",
    "ENTRY_MARKER",
    "LANES",
    "AnalysisEntry",
    "AnalysisWriteFailed",
    "analysis_path",
    "append_entry",
    "build_package",
    "describe_existing",
    "file_size",
    "partition_items",
    "read_entries",
    "untrusted_block",
]
