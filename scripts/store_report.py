"""Classify everything under the output root by provenance. Moves nothing.

Ruling R1 / D-142 split the output root into trees by WHO WANTED A FILE TO
EXIST. Almost all of that migration needs no action: legacy store roots stay
readable, the glossary and filler list are adopted on first touch, and the
platform folders were already correct. What is left is the handful of things
nothing owns -- a source file dropped at the root, a directory from a layout
nobody remembers -- and this prints them so a person can decide.

Deliberately a script and not a CLI verb. The PSM proposed `mfp store-report`;
ruling R9 asks mfp to stay light and correctly tiered, and a one-time
migration aid that becomes permanent product surface is the opposite of that.
It is also not a `doctor` check: `doctor` answers「is a dependency missing」
with ok/required semantics, and a stray file is neither failing nor required.

    python scripts/store_report.py [--root D:/output/MediaGrabbed] [--json]

Read-only by construction: this module imports nothing that writes and calls
no function that creates a directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfp import runs  # noqa: E402
from mfp.config import load_config  # noqa: E402

#: What a person should do with each class, in the words the ruling used.
ADVICE = {
    "A": "manual download -- already where it belongs, leave it",
    "C": "analysis store -- already where it belongs",
    "C-legacy": "an older analysis layout; read and indexed, never written to again",
    "D": "refined reference -- adopted into _reference/ on first use, original kept",
    "E": "engine asset -- not output at all; moving it is a separate decision",
    "F": "a source you supplied; its home is _inbox/, move it there when convenient",
    "?": "nothing claims this -- decide and then tell the rules about it",
}


def classify(root: Path, entry: Path) -> str:
    name = entry.name
    if entry.is_file():
        # A file directly at the root is either a reference list this build
        # knows by name, or something the user dropped here to be analysed.
        if name in {"_glossary.json", "_fillers.json"}:
            return "D"
        return "F"
    if name == runs.STORE_DIR:
        return "C"
    if name in runs.LEGACY_STORE_DIRS:
        return "C-legacy"
    if name == runs.REFERENCE_DIR:
        return "D"
    if name == runs.INBOX_DIR:
        return "F"
    if name == "_models":
        return "E"
    if name.startswith("_"):
        return "?"
    return "A"


def survey(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    rows = []
    for entry in sorted(root.iterdir()):
        kind = classify(root, entry)
        size = entry.stat().st_size if entry.is_file() else None
        rows.append({
            "name": entry.name,
            "kind": "file" if entry.is_file() else "dir",
            "provenance": kind,
            "bytes": size,
            "advice": ADVICE[kind],
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="Output root (default: config)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else Path(load_config().output_root)
    rows = survey(root)

    if args.json:
        print(json.dumps({"root": str(root), "entries": rows}, ensure_ascii=False, indent=1))
        return 0

    if not rows:
        print(f"{root} does not exist or is empty -- nothing to classify")
        return 0

    print(f"{root}\n")
    width = max(len(row["name"]) for row in rows)
    for row in rows:
        size = "" if row["bytes"] is None else f"  {row['bytes'] / 1e6:,.1f} MB"
        print(f"  [{row['provenance']:<8}] {row['name']:<{width}}{size}")
        print(f"  {'':<11}{row['advice']}")

    homeless = [row for row in rows if row["provenance"] in {"F", "?"} and row["kind"] == "file"]
    if homeless:
        print(f"\n{len(homeless)} file(s) with no home. Suggested: {runs.inbox(root)}")
        print("Nothing has been moved. Move them yourself, or leave them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
