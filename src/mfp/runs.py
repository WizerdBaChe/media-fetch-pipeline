"""Where one analysis keeps its files.

Everything `mfp transcript`, `mfp translate` and `mfp correct` produce used to
land flat in `<outputRoot>/_captions/`, named `<stem>-<sha1[:8]>.<lang>.srt`.
Two complaints killed that layout, and both were the user's (2026-08-28):

  * the eight hex characters read as neither a timestamp nor a hash, so they
    looked like noise attached to every file the product made;
  * eight files per recording, all sharing a prefix, in one growing folder.

What replaces it is one folder per ANALYSIS, named after the recording and
separated by the time the analysis ran, with the two kinds of file a person
actually distinguishes in their own subfolder::

    <outputRoot>/逐字稿/老師的設計講解_2026-08-28_1432/
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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mfp.naming import sanitize_component

__all__ = [
    "MARKER",
    "Run",
    "SUBTITLES_DIR",
    "TEXTS_DIR",
    "TRANSCRIPTS_DIR",
    "find",
    "home",
    "key_for",
    "open_run",
    "place",
    "record_transcript",
    "run_of",
    "workspace_for",
]

#: The three folder names a person reads. Chinese because the people who open
#: this folder in Explorer read Chinese, and the GUI already calls these
#: things 逐字稿, 字幕檔 and 文字檔 -- one vocabulary, on screen and on disk.
TRANSCRIPTS_DIR = "逐字稿"
SUBTITLES_DIR = "字幕檔"
TEXTS_DIR = "文字檔"

MARKER = "_source.json"
SCHEMA_VERSION = 1

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

    def captions(self, language: str | None = None) -> list[Path]:
        """The transcript files this run recorded, still on disk.

        Recorded rather than globbed. A folder holds the original transcript,
        its translations and its corrections, all sharing a stem, and a glob
        would happily hand `talk.zh.corrected.srt` back as the transcript to
        correct. `_source.json` says which files are transcripts because
        something WROTE them as one.
        """
        rows = self.read_marker().get("transcripts") or {}
        if not isinstance(rows, dict):
            return []
        wanted = [lang for lang in rows if language is None or lang == language]
        if language is not None and not wanted:
            wanted = list(rows)
        out = []
        for lang in wanted:
            candidate = self.root / str(rows[lang])
            if candidate.is_file():
                out.append(candidate)
        return out


def home(output_root: str | Path) -> Path:
    """`<outputRoot>/逐字稿` -- where every run folder lives."""
    return Path(output_root).expanduser() / TRANSCRIPTS_DIR


def place(root: str | Path, name: str) -> Path:
    """Route one file name into its subfolder under `root`, and mkdir it.

    The routing is by extension and by nothing else, so a caller does not
    have to know the layout to write into it correctly. It is applied to
    `--out` as well: one rule with no exceptions beats a layout that depends
    on which flag put you there.
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
    for candidate in (here, *here.parents):
        if (candidate / MARKER).is_file():
            return Run(candidate)
        if candidate.name == TRANSCRIPTS_DIR:
            break
    return None


def find(output_root: str | Path, source: str | Path) -> Run | None:
    """The newest run made from this source, or None.

    Reads every marker under `逐字稿/`. That is a few dozen small files on a
    real machine and it happens once per read, against an operation that
    costs minutes when it misses -- an index would be faster and would also
    be a second thing that can be wrong about what is on disk.
    """
    key = key_for(source)
    base = home(output_root)
    if not base.is_dir():
        return None
    found: list[tuple[str, Path]] = []
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
    kind: str = "media",
    when: datetime | None = None,
) -> Run:
    """Start a new analysis folder for this source. Always a new one.

    `find` is what reuses an existing analysis; this only ever creates. The
    separation is deliberate: a `--refresh` is a second analysis of the same
    audio and the first one's transcript is not garbage to be overwritten --
    it is what the user may have already corrected by hand.
    """
    base = home(output_root)
    base.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now()
    safe = sanitize_component(stem, fallback="transcript")[:_MAX_STEM].strip(". ")
    label = f"{safe or 'transcript'}_{when.strftime('%Y-%m-%d_%H%M')}"

    folder = base / label
    serial = 2
    while folder.exists():
        # Same recording, same minute -- two analyses started back to back.
        # A suffix beats writing into the other one's folder.
        folder = base / f"{label}-{serial}"
        serial += 1
    folder.mkdir(parents=True)

    run = Run(folder)
    _write_marker(run, {
        "schemaVersion": SCHEMA_VERSION,
        "source": str(source),
        "key": key_for(source),
        "kind": kind,
        "created": when.isoformat(timespec="seconds"),
        "transcripts": {},
    })
    return run


def workspace_for(
    output_root: str | Path, source: str | Path, *, kind: str = "caption"
) -> Run:
    """The run folder a DERIVED file should be written into.

    Translation and correction both act on a transcript that already exists,
    and their output belongs with it. Three cases, in order: the source is
    already inside a run folder (the usual one -- we made it); some earlier
    run was made from it; or it is a caption file the user handed us from
    anywhere, which gets a folder of its own. The last case is the reason
    this exists at all -- writing a corrected copy next to somebody's own
    file is not ours to do.
    """
    here = Path(source).expanduser()
    return (
        run_of(here)
        or find(output_root, here)
        or open_run(output_root, here, stem=here.stem, kind=kind)
    )


def record_transcript(run: Run, path: Path, language: str | None = None) -> None:
    """Note that `path` is a TRANSCRIPT of this run's source.

    Called by whatever produced it. Everything else in the folder -- the
    translations, the corrected copies, the diff -- is derived from one of
    these, and only these are what a later read may hand back as the cached
    answer.

    **Derived artifacts are deliberately NOT recorded here** (F4 audit gap
    G4, ruled 2026-08-30). The audit's observation is correct -- a
    translation, a corrected copy and a tidied copy are findable by filename
    convention alone -- and the answer is still no, for three reasons worth
    writing down so the next audit does not re-raise it:

      * This map answers exactly one question: which file may be handed back
        as the cached transcript for this source (`Run.captions`). A derived
        file must never answer it -- `talk.eng_Latn.srt` returned as the
        transcript of the audio is a wrong transcript, and a wrong transcript
        looks exactly like a right one (D-122). Adding them would therefore
        need a SECOND key, and a key nothing reads is worse than no key
        (P-57).
      * Every derived set already carries its own record, filed under one
        serial with the file it describes: `*.corrections.json`,
        `*.tidy.json`, `*.translation.json`. That record names its source,
        its engine and its numbers -- strictly more than a marker entry could
        hold -- and `runs.place_set` is what stops it drifting from its
        subject (P-62). The marker would be a second, weaker copy of it.
      * `--out` can send a derived file anywhere, including outside any run
        folder, so this map could only ever be a PARTIAL index that reads as
        a complete one. D-122 rejected an index for that exact reason: a
        second thing that can be wrong about what is on disk.

    Reopen this if something ever needs to enumerate derived files WITHOUT
    reading the folder -- that is the trigger, and nothing has it today.
    """
    marker = run.read_marker()
    rows = marker.get("transcripts")
    if not isinstance(rows, dict):
        rows = {}
    try:
        relative = str(Path(path).relative_to(run.root)).replace("\\", "/")
    except ValueError:
        return
    rows[str(language or "und")] = relative
    marker["transcripts"] = rows
    _write_marker(run, marker)


def _read_marker(folder: Path) -> dict:
    try:
        payload = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_marker(run: Run, payload: dict) -> None:
    run.root.mkdir(parents=True, exist_ok=True)
    run.marker.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
