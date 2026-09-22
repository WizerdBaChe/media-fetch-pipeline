"""The analysis store: where anything that exists BECAUSE of an analysis lives.

This module used to be「where a transcript goes」. It is now the store for
every provenance class that is not a manual download (D-142): what an
analysis verb produced, and what it had to fetch in order to produce it. The
criterion is who wanted the file to exist, never what its extension is
(`INV-P10`) -- `stack`'s working video and `brief`'s images are analysis
input, and they were landing in the download tree indistinguishable from
something the user had asked for by name.

Two roots sit beside the store and are named here because they are the same
decision: `_inbox` for a source the user supplied themselves (there was a
74 MB `.aac` at the output root with no rule governing it), and `_reference`
for material a user has refined and later work may cite -- the glossary and
the filler list, which had been sitting flat among rough output.

`逐字稿/` and `_captions/` become LEGACY store roots: read, enumerated by the
index, never written to again. That is the repair for P-60, which orphaned
`_captions/` by simply ceasing to read it.

---

Where one analysis keeps its files.

Everything an analysis verb produced used to land flat in
`<outputRoot>/_captions/`, named `<stem>-<sha1[:8]>.<lang>.srt`.
Two complaints killed that layout, and both were the user's (2026-08-28):

  * the eight hex characters read as neither a timestamp nor a hash, so they
    looked like noise attached to every file the product made;
  * eight files per recording, all sharing a prefix, in one growing folder.

What replaces it is one folder per ANALYSIS, named after the recording and
separated by the time the analysis ran, with the two kinds of file a person
actually distinguishes in their own subfolder::

    <outputRoot>/分析/老師的設計講解_2026-08-28_1432/
        _source.json                       ← what this was made from
        字幕檔/  老師的設計講解.zh.srt      ← for a player, and for 引用長圖
                 老師的設計講解.zh.corrected.srt
        文字檔/  老師的設計講解.zh.txt      ← for a person to read
                 老師的設計講解.zh.corrected.txt
                 老師的設計講解.zh.corrections.diff.txt
        老師的設計講解.zh.corrections.json  ← the machine record

The digest is gone from every name a person sees, and the collision it was
there to prevent is prevented by something better: a re-analysis never writes
into an existing folder at all, so a second `recording.mp3` from another
directory cannot overwrite the first one's words, and neither can a
`--refresh` of the same file. Nothing here ever replaces a file it did not
write in this run.

Identity lives in `_source.json` rather than in the folder name. That is what
lets the name be readable: the cache is keyed on the resolved source path (or
the URL), which is exact, while the folder is named for a human, which is not.
A user who renames a folder loses the cache hit and pays for a re-analysis --
never a wrong transcript.

---

**Nothing in this module knows what a transcript is, since 2026-09-09.**
`record_transcript` and `Run.captions` moved out to their own module by user
ruling (「拆成兩份」), and that whole family was later removed with it
(2026-09-16). The handoff card for `local-transcript-maker` §5 named this
module as the single thing blocking the family's extraction -- the import
graph called it CORE and was right, while its content was transcript-shaped,
and an import graph cannot see that by construction. The split is what made
the removal possible without touching this module a second time.

What is left here is a store, in the sense a filesystem is: it holds what
verbs produce without knowing what any of them are. 字幕檔 and 文字檔 stay,
and that is not an exception -- they name a LAYOUT the user ruled (D-122),
`place` routes every analysis verb's output through it, and `brief` writes
into 文字檔 without being a recognition verb. Routing a suffix is not knowing
what a file means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mfp.naming import sanitize_component

__all__ = [
    "INBOX_DIR",
    "LEGACY_STORE_DIRS",
    "MARKER",
    "REFERENCE_DIR",
    "Run",
    "STORE_DIR",
    "SUBTITLES_DIR",
    "TEXTS_DIR",
    "TIER_ASSET",
    "TIER_RAW",
    "adopt_reference",
    "canonical_post_key",
    "discard_if_untouched",
    "download_tree_roots",
    "find",
    "home",
    "inbox",
    "key_for",
    "legacy_roots",
    "open_run",
    "place",
    "reference_home",
    "refuse_download_tree",
    "run_of",
    "store_roots",
    "tier_of",
    "verb_of",
    "workspace_for",
    "write_marker",
]

#: The folder names a person reads. Chinese because the people who open this
#: folder in Explorer read Chinese, and the GUI already calls these things
#: 字幕檔 and 文字檔 -- one vocabulary, on screen and on disk.
#:
#: `STORE_DIR` replaced `TRANSCRIPTS_DIR = "逐字稿"` (D-142): the old name
#: described a FORMAT, and this tree now holds every analysis whatever its
#: verb produced. A `brief` run holds images and an explanation; naming its
#: parent 逐字稿 would be false on disk, where names are the one thing a
#: person actually reads.
STORE_DIR = "分析"
SUBTITLES_DIR = "字幕檔"
TEXTS_DIR = "文字檔"

#: Stores that are read and never written. `逐字稿` is this layout's own
#: previous name; `_captions` is the flat layout D-122 replaced. Both are
#: enumerated by `mfp.index` so nothing that was analysed becomes invisible
#: -- P-60 orphaned `_captions` by just not reading it any more, and the
#: files are still there.
LEGACY_STORE_DIRS = ("逐字稿", "_captions")

#: Class F -- a source the user supplied rather than downloaded.
INBOX_DIR = "_inbox"

#: Class D -- refined material later work may cite. Rough analysis output is
#: NOT this; that is the whole distinction (`tier` below, ruling R10).
REFERENCE_DIR = "_reference"

MARKER = "_source.json"

#: 2 adds `verb` and `tier`. A marker without them was written before
#: D-142, when `transcript` was the only verb that could have written one --
#: which is exactly what the readers below assume, and why no migration runs.
SCHEMA_VERSION = 2

#: Ruling R10 / D-148. `raw` = a first pass, cleaned raw data. `asset` = a
#: second pass, worth citing, and living in a store this project does not
#: own. mfp records which one and nothing more (`INV-P11`).
TIER_RAW = "raw"
TIER_ASSET = "asset"

#: Which subfolder a file goes in, by extension. Anything not named here
#: stays in the run folder itself -- the machine record `corrections.json` is
#: neither a subtitle nor something to read, and inventing a third folder for
#: one file would be worse than leaving it at the top.
_SUBTITLE_SUFFIXES = (".srt", ".vtt")
_TEXT_SUFFIXES = (".txt", ".md", ".markdown")

#: Folder names are `<stem>_<when>`, and the stem is cut well short of
#: `sanitize_component`'s own 80 so the timestamp, the subfolder and the file
#: name all still fit inside Windows' 260-character path limit.
_MAX_STEM = 60


@dataclass(frozen=True)
class Run:
    """One analysis's folder. Cheap to construct; touches no disk until asked."""

    root: Path

    @property
    def subtitles(self) -> Path:
        return self.root / SUBTITLES_DIR

    @property
    def texts(self) -> Path:
        return self.root / TEXTS_DIR

    @property
    def marker(self) -> Path:
        return self.root / MARKER

    def path_for(self, name: str) -> Path:
        """Where a file of this name belongs, with its folder created."""
        return place(self.root, name)

    def read_marker(self) -> dict:
        return _read_marker(self.root)


def home(output_root: str | Path) -> Path:
    """`<outputRoot>/分析` -- where every run folder lives."""
    return Path(output_root).expanduser() / STORE_DIR


def legacy_roots(output_root: str | Path) -> list[Path]:
    """Store roots that are read and never written, newest layout first.

    Only the ones that actually exist: an absent legacy root is not a
    condition anything should have to handle, and returning it would make
    every caller check `is_dir()` a second time.
    """
    base = Path(output_root).expanduser()
    return [base / name for name in LEGACY_STORE_DIRS if (base / name).is_dir()]


def store_roots(output_root: str | Path) -> list[Path]:
    """Every root a run may be found in: the live store first, then legacy."""
    return [home(output_root), *legacy_roots(output_root)]


def inbox(output_root: str | Path) -> Path:
    """`<outputRoot>/_inbox` -- class F, a source the user supplied."""
    return Path(output_root).expanduser() / INBOX_DIR


def reference_home(output_root: str | Path) -> Path:
    """`<outputRoot>/_reference` -- class D, refined material worth citing."""
    return Path(output_root).expanduser() / REFERENCE_DIR


def adopt_reference(output_root: str | Path, filename: str) -> Path:
    """The canonical path of a reference file, adopting a legacy copy once.

    The glossary and the filler list were sitting flat at the output root
    among rough analysis output, which is a provenance error: they are the
    one thing here a user has REFINED by hand, and D-142 gives them their own
    root. Callers read and write the canonical path only.

    **Adoption copies and never moves.** These two files decide what the
    corrector is allowed to write into a transcript; a half-completed move
    would leave the user with no glossary and a corrector that silently
    stops proposing anything it used to know. The old file stays exactly
    where it was, and the next session to look will find it unchanged.

    This function is named for the side effect rather than hiding it behind
    a `*_path()` that looks pure -- a path helper that copies a file is how
    the copy ends up happening somewhere nobody expected.
    """
    canonical = reference_home(output_root) / filename
    if canonical.exists():
        return canonical
    legacy = Path(output_root).expanduser() / filename
    if legacy.is_file():
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(legacy.read_bytes())
    return canonical


def place(root: str | Path, name: str) -> Path:
    """Route one file name into its subfolder INSIDE one run, and mkdir it.

    **This is not the provenance judge.** Which TREE a file belongs to is
    decided by who wanted it to exist, declared by the writer and never
    inferred from a suffix (`INV-P10`, D-142). What this function answers is
    the much smaller question of how the files of ONE analysis are arranged
    once they are already in its folder -- 字幕檔 and 文字檔, a shape the
    user ruled directly (D-122) and D-142 deliberately left alone.

    Within that question the routing is by extension and by nothing else, so
    a caller does not have to know the layout to write into it correctly. It
    is applied to `--out` as well: one rule with no exceptions beats a layout
    that depends on which flag put you there.

    The previous wording of this docstring said only「by extension and by
    nothing else」, which reads as a cross-tree rule and contradicts
    `INV-P10`. That contradiction was found by the PIM's own gap register
    (V-1) before it could teach anyone the wrong criterion.
    """
    root = Path(root)
    suffix = Path(name).suffix.lower()
    if suffix in _SUBTITLE_SUFFIXES:
        folder = root / SUBTITLES_DIR
    elif suffix in _TEXT_SUFFIXES:
        folder = root / TEXTS_DIR
    else:
        folder = root
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


def place_set(root: str | Path, names: dict[str, str]) -> dict[str, Path]:
    """Route a SET of names, none of which may land on an existing file.

    The create-only rule `open_run` applies to folders, applied one level
    down to the files inside them. It was missing there: measured
    2026-08-30, a second `mfp tidy --apply` silently replaced the first
    tidied copy, hand edits included. A derived file is cheap to regenerate
    and the user's edit of one is not.

    **One serial for the whole set**, which is the reason this takes a dict
    rather than being called once per file. `correct` writes four files and
    `tidy` three; letting each pick its own free name would file a record
    under `.tidy.json` describing an output that had gone to
    `.tidy-2.srt` -- a record that no longer matches its subject is worse
    than the overwrite it was avoiding.

    Returns `{key: path}` with the same keys it was given.
    """
    root = Path(root)
    routed = {key: place(root, name) for key, name in names.items()}
    if not any(path.exists() for path in routed.values()):
        return routed

    serial = 2
    while True:
        candidate = {
            key: path.with_name(f"{path.stem}-{serial}{path.suffix}")
            for key, path in routed.items()
        }
        if not any(path.exists() for path in candidate.values()):
            return candidate
        serial += 1


def place_new(root: str | Path, name: str) -> Path:
    """`place`, but never onto a file that is already there."""
    return place_set(root, {"only": name})["only"]


def key_for(source: str | Path) -> str:
    """The identity a run is cached on.

    A URL is itself. A local file is its resolved path, lower-cased, because
    Windows paths are case-insensitive: `Talk.mp3` and `talk.MP3` are one
    file, and a key that distinguished them would transcribe the same audio
    twice and call the second one new.
    """
    text = str(source)
    if "://" in text:
        return text.strip()
    return str(Path(text).expanduser().resolve()).lower()


def run_of(path: str | Path) -> Run | None:
    """The run folder a file already lives in, or None if it is not in one.

    Used by the verbs that act on an EXISTING transcript: a correction of
    `逐字稿/talk_.../字幕檔/talk.zh.srt` belongs in that same analysis, not in
    a new one. Walks up rather than assuming a depth, so a file at the run
    root and a file in a subfolder both resolve.
    """
    here = Path(path).expanduser()
    # A directory is where the walk starts; anything else is a file, and the
    # walk starts at its folder. `is_dir()` is asked FIRST because a run
    # folder can carry a dot -- `talk.zh_2026-08-29_1432` comes from a
    # caption file the user named -- and deciding on the suffix alone would
    # step over the very folder being asked about.
    if not here.is_dir():
        here = here.parent
    stop_names = {STORE_DIR, *LEGACY_STORE_DIRS}
    for candidate in (here, *here.parents):
        if (candidate / MARKER).is_file():
            return Run(candidate)
        # Stop at any store root, live or legacy: walking past one would
        # start testing the output root and then the whole drive.
        if candidate.name in stop_names:
            break
    return None


def download_tree_roots(output_root: str | Path) -> list[Path]:
    """The platform folders `fetch` owns -- everything else under the root
    belongs to the store, the inbox, the reference root, or the engine."""
    base = Path(output_root).expanduser()
    if not base.is_dir():
        return []
    reserved = {STORE_DIR, INBOX_DIR, REFERENCE_DIR, *LEGACY_STORE_DIRS}
    return [
        child for child in base.iterdir()
        if child.is_dir() and child.name not in reserved
        and not child.name.startswith("_")
    ]


def refuse_download_tree(output_root: str | Path, candidate: str | Path) -> Path:
    """`candidate` resolved, refusing only a path inside the DOWNLOAD TREE.

    `INV-P3`, narrowed during M2 from what the PIM first wrote. The first
    version refused any `--out` outside a store, and implementing it showed
    the rule fighting the user: `mfp stack --out D:\\MyNotes\\quote.png` is an
    ordinary export of a finished product, not pollution, and refusing it
    would make the invariant an obstacle rather than a protection.

    What ruling R1 actually objects to is analysis output MIXED IN with manual
    downloads, where nothing on disk can tell the two apart afterwards
    (`INV-P10`: provenance is not recoverable from a file). That is exactly
    and only the download tree, so that is exactly and only what is refused.

    D-136's index objection survives the narrowing because the index tracks
    RUNS, not derived files: an exported copy leaves the run in the store,
    so run-level completeness is untouched.
    """
    from mfp.errors import OutsideStore

    resolved = Path(candidate).expanduser().resolve()
    for tree in download_tree_roots(output_root):
        tree = tree.resolve()
        if resolved == tree or tree in resolved.parents:
            raise OutsideStore(
                f"{candidate} is inside the download tree ({tree}), which holds "
                "only what you asked to download by name. Analysis output kept "
                "there cannot be told apart from a download afterwards.",
                path=str(resolved),
            )
    return resolved


def canonical_post_key(platform: str, post_id: str) -> str:
    """The identity of a POST, independent of how its URL was spelled.

    `key_for` treats a URL as itself, which is right for a caption file and
    wrong for a post: `.../p/ABC/`, `.../p/ABC` and the same link carrying a
    tracking query are one post, and keying on the string would fetch it
    three times. The old layout got this right by accident -- it globbed the
    download tree for `*_<postId>` -- and that mechanism went away with the
    tree (D-143), so the identity has to be explicit now.
    """
    return f"mfp:post:{platform}:{post_id}"


def find(output_root: str | Path, source: str | Path, *, key: str | None = None) -> Run | None:
    """The newest run made from this source, or None.

    Reads every marker under the store AND every legacy root, so an analysis
    made before D-142 renamed the tree is still found rather than silently
    redone. That is a few dozen small files on a real machine, once per read,
    against an operation that costs minutes when it misses.

    `mfp.index` exists now and is deliberately NOT consulted here: it is a
    cache (`INV-P7`) and this is the path where being wrong costs the user a
    re-recognition. The disk is the truth; the index is for answering「有沒有
    分析過」without paying this walk.
    """
    key = key or key_for(source)
    found: list[tuple[str, Path]] = []
    for base in store_roots(output_root):
        if not base.is_dir():
            continue
        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            marker = _read_marker(folder)
            if marker.get("key") == key:
                found.append((str(marker.get("created") or ""), folder))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], row[1].name))
    return Run(found[-1][1])


def open_run(
    output_root: str | Path,
    source: str | Path,
    *,
    stem: str,
    verb: str,
    kind: str = "media",
    when: datetime | None = None,
    key: str | None = None,
) -> Run:
    """Start a new analysis folder for this source. Always a new one.

    `find` is what reuses an existing analysis; this only ever creates. The
    separation is deliberate: a `--refresh` is a second analysis of the same
    audio and the first one's transcript is not garbage to be overwritten --
    it is what the user may have already corrected by hand.

    `verb` is required and has no default. This tree now holds runs from
    several verbs (D-142) and the folder name cannot say which -- names are
    for people and load-bearing for nothing (`INV-P4`). A default would pick
    a producing verb for a caller that forgot to, and the index would then
    report it as fact.
    """
    base = home(output_root)
    base.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now()
    safe = sanitize_component(stem, fallback="analysis")[:_MAX_STEM].strip(". ")
    label = f"{safe or 'analysis'}_{when.strftime('%Y-%m-%d_%H%M')}"

    folder = base / label
    serial = 2
    while folder.exists():
        # Same recording, same minute -- two analyses started back to back.
        # A suffix beats writing into the other one's folder.
        folder = base / f"{label}-{serial}"
        serial += 1
    folder.mkdir(parents=True)

    run = Run(folder)
    write_marker(run, {
        "schemaVersion": SCHEMA_VERSION,
        "source": str(source),
        "key": key or key_for(source),
        "kind": kind,
        "verb": verb,
        "tier": TIER_RAW,
        "created": when.isoformat(timespec="seconds"),
        "transcripts": {},
    })
    return run


def discard_if_untouched(run: Run) -> bool:
    """Undo an `open_run` that produced nothing. Returns whether it did.

    The one deleting path in this module, and it exists because the other
    rule here is stronger than it looks: a run folder that exists is a CLAIM
    that an analysis happened (`open_run`'s own comment), and `mfp analyzed`
    reads the tree as fact. A cancelled transcription that left its folder
    behind would put an analysis with no transcript into that list forever,
    and the user cannot tell it from one whose files went missing.

    Deliberately narrow, because deleting is not this module's job: it
    removes the marker and the folder, and ONLY when the folder holds
    nothing else -- no subdirectory, no file, not even an empty 字幕檔/.
    Anything else means something got written after all, and a partial
    result is the user's to look at and delete. The refusal branch is
    exercised on purpose (`test_runs.py`), because a destructive path that
    has never been run is a destructive path nobody has checked (P-78).
    """
    if not run.root.is_dir():
        return False
    leftovers = [entry for entry in run.root.iterdir() if entry.name != MARKER]
    if leftovers:
        return False
    run.marker.unlink(missing_ok=True)
    try:
        run.root.rmdir()
    except OSError:
        # Something appeared between the listing and the removal, or the
        # folder is open in Explorer. Leaving it is the safe answer: the
        # cost is one empty folder, and the alternative is a recursive
        # delete on a path that just proved it is not what we measured.
        return False
    return True


def verb_of(marker: dict) -> str:
    """The producing verb a marker records.

    A marker written before schema 2 has no `verb`, and the answer for those
    is `transcript` as a FACT, not a fallback: `open_run` had exactly one
    caller then. This is why no migration runs over the store -- the missing
    field is not ambiguous.
    """
    recorded = marker.get("verb")
    return recorded if isinstance(recorded, str) and recorded else "transcript"


def tier_of(marker: dict) -> str:
    """`raw` or `asset` (ruling R10).

    Unknown values read as `raw`. A promotion is something a person did on
    purpose and recorded; anything this module cannot recognise has not been
    promoted, and calling an unreadable value `asset` would put a rough
    first pass into the one tier that means「worth citing」.
    """
    recorded = marker.get("tier")
    return TIER_ASSET if recorded == TIER_ASSET else TIER_RAW


def workspace_for(
    output_root: str | Path,
    source: str | Path,
    *,
    kind: str = "caption",
    verb: str = "transcript",
) -> Run:
    """The run folder a DERIVED file should be written into.

    Translation and correction both act on a transcript that already exists,
    and their output belongs with it. Three cases, in order: the source is
    already inside a run folder (the usual one -- we made it); some earlier
    run was made from it; or it is a caption file the user handed us from
    anywhere, which gets a folder of its own. The last case is the reason
    this exists at all -- writing a corrected copy next to somebody's own
    file is not ours to do.

    `verb` defaults here and does not in `open_run`, and the difference is
    real: this function is only reached by verbs acting on an EXISTING
    transcript, so the third case is a transcript workspace being opened
    late. A caller wanting a different producing verb says so.
    """
    here = Path(source).expanduser()
    return (
        run_of(here)
        or find(output_root, here)
        or open_run(output_root, here, stem=here.stem, kind=kind, verb=verb)
    )


def _read_marker(folder: Path) -> dict:
    try:
        payload = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_marker(run: Run, payload: dict) -> None:
    """Replace this run's `_source.json` with `payload`.

    Public rather than reached for through the underscore, so the store's
    surface says what another module is allowed to do with a marker.
    """
    run.root.mkdir(parents=True, exist_ok=True)
    run.marker.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
