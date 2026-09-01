"""What has been analysed. Routing only -- never what the analysis said.

Ruling R2 (2026-09-01): global retrieval answers「有沒有分析過這件事」and
shows no content unless the reader follows the pointer. The reason is in the
ruling itself: a first-pass analysis is rough, and the material worth citing
is the refined kind, so an index that quoted a rough sentence out of its
folder would present a draft as a finding.

**Five columns, and every one of them is routing** (`INV-P6`)::

    source_key   what was analysed
    verb         which analysis
    pointer      where the run folder is
    tier         raw (a first pass) or asset (a promoted one, ruling R10)
    promoted_to  where a promoted run went; opaque to this module

There is no summary column, no first line, no excerpt, and adding one is a
breach rather than a feature. `tier` and `promoted_to` are still routing: they
say which SHELF a thing is on, not what is written on it.

**This is a cache and never a truth** (`INV-P7`). D-122 rejected an index
because it would be "a second thing that can be wrong about what is on disk",
and that objection is answered by refusing to let it hold a wrong belief:
stale, missing or unparseable all mean rebuild, and there is no repair path,
no merge and no partial write to reconcile. The disk is the only source of
truth, which is also why `runs.find` -- the path where being wrong costs the
user a re-recognition -- does not consult this module at all.

**mfp does not know what an asset store is** (`INV-P11`). `promoted_to` is an
opaque string. The four-store taxonomy (user ruling 2026-08-16) already
assigns concrete assets, task-triggered domains and abstract concepts to
owners with their own governance; a copy of that knowledge here would go
stale, which is the failure `INV-P9` exists to prevent one layer down.
Nothing in this module writes `promoted_to` -- the second pass is not built
(D-148), and this is only the column that makes it expressible.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from mfp import runs

__all__ = [
    "CACHE_FILE",
    "IndexEntry",
    "lookup",
    "rebuild",
    "unreadable_markers",
]

#: Lives inside the store it describes, so moving the store moves its cache
#: and a stale one can never be read against a different tree.
CACHE_FILE = "_index.json"

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class IndexEntry:
    """One analysis run, as routing. See the module docstring for why these
    are the only fields there will be."""

    source_key: str
    verb: str
    pointer: str
    tier: str = runs.TIER_RAW
    promoted_to: str | None = None

    def as_row(self) -> dict:
        return asdict(self)


def _entry_for(folder: Path) -> IndexEntry | None:
    marker = runs.Run(folder).read_marker()
    key = marker.get("key")
    if not isinstance(key, str) or not key:
        return None
    promoted = marker.get("promotedTo")
    return IndexEntry(
        source_key=key,
        verb=runs.verb_of(marker),
        pointer=str(folder),
        tier=runs.tier_of(marker),
        promoted_to=promoted if isinstance(promoted, str) and promoted else None,
    )


def unreadable_markers(output_root: str | Path) -> int:
    """How many run folders could not be read on the last walk.

    Reported rather than swallowed. D-136's objection to indexing was a
    PARTIAL map that reads as a complete one, so a walk that skipped
    something has to be able to say how much.
    """
    return _walk(output_root)[1]


def _walk(output_root: str | Path) -> tuple[list[IndexEntry], int]:
    entries: list[IndexEntry] = []
    skipped = 0
    for base in runs.store_roots(output_root):
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir():
                continue
            if not (folder / runs.MARKER).is_file():
                # A legacy root holds loose files as well as run folders --
                # `_captions/` is nothing but loose files. Not an error.
                continue
            entry = _entry_for(folder)
            if entry is None:
                skipped += 1
                continue
            entries.append(entry)
    return entries, skipped


def rebuild(output_root: str | Path, *, say=None) -> list[IndexEntry]:
    """Walk the store and every legacy root, and write the cache.

    `say` receives one line when markers were skipped. The caller decides
    where that goes; nothing here prints on its own.
    """
    entries, skipped = _walk(output_root)
    if skipped and say is not None:
        say(
            f"{skipped} run folder(s) had an unreadable {runs.MARKER} and are "
            "missing from this listing"
        )

    home = runs.home(output_root)
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / CACHE_FILE).write_text(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "entries": [entry.as_row() for entry in entries],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        # A cache that cannot be written is not a failure of the question
        # being asked. The answer is already in hand; the next call simply
        # pays for the walk again.
        pass
    return entries


def _cached(output_root: str | Path) -> list[IndexEntry] | None:
    """The cache, if it exists and is no older than the newest run marker.

    Returns None for missing, unparseable, wrong-schema and stale alike --
    they all have the same remedy, and giving them separate handling is how a
    cache acquires a repair path and becomes a second source of truth.
    """
    path = runs.home(output_root) / CACHE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cached_at = path.stat().st_mtime
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        return None

    for base in runs.store_roots(output_root):
        if not base.is_dir():
            continue
        for folder in base.iterdir():
            marker = folder / runs.MARKER
            try:
                if marker.is_file() and marker.stat().st_mtime > cached_at:
                    return None
            except OSError:
                return None

    rows = payload.get("entries")
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            out.append(IndexEntry(**row))
        except TypeError:
            # A row shaped for another version. Rebuild rather than guess.
            return None
    return out


def entries(output_root: str | Path, *, say=None) -> list[IndexEntry]:
    """Every analysis run, from the cache when it is current."""
    cached = _cached(output_root)
    if cached is not None:
        return cached
    return rebuild(output_root, say=say)


def lookup(
    output_root: str | Path, source: str | Path, *, key: str | None = None, say=None
) -> list[IndexEntry]:
    """Every run made from this source, newest folder name last."""
    wanted = key or runs.key_for(source)
    return [row for row in entries(output_root, say=say) if row.source_key == wanted]


def _say(message: str) -> None:
    print(message, file=sys.stderr)
