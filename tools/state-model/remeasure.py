"""Re-derive every line count in `model.json` from the repository.

**This is not part of the gate, and it must never be run by it.** `verify.py`
rung 0 exists to notice that the diagram's numbers have gone stale; a checker
that regenerates the claim it is about to check can only ever pass. So this is
a separate, deliberate act: somebody looks at a rung-0 FAIL, decides the shape
did not change and only the sizes did, and re-measures.

It prints what moved and by how much, because that list is the point -- a
module that grew 400 lines between two snapshots is worth seeing, and a module
that appeared or vanished is a STRUCTURAL change this script deliberately
refuses to paper over: it exits non-zero when a file the model names is gone.

After running this, run `build.py` -- the model is canonical but the page is
what `verify.py` rung 1 reads, and a refreshed model with a stale page is the
same staleness one layer along.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path(r"D:/AIWork/media-fetch-pipeline")

M = json.loads((HERE / "model.json").read_text(encoding="utf-8"))
zones = {z["id"]: z for z in M["zones"]}


def wc(p: pathlib.Path) -> int:
    return len(p.read_text(encoding="utf-8", errors="replace").splitlines())


def wc_dir(rel: str) -> int:
    """Mirrors `verify.py`'s rule exactly -- `.ts`/`.tsx`, tests excluded.

    Duplicated on purpose rather than imported: if the two ever disagree the
    gate fails, which is the outcome you want. A shared helper would make them
    agree by construction and the disagreement is the signal.
    """
    d = REPO / rel
    if not d.is_dir():
        return -1
    return sum(
        wc(p)
        for p in sorted(d.rglob("*"))
        if p.suffix in (".ts", ".tsx")
        and ".test." not in p.name
        and ".spec." not in p.name
    )


LOCATE = {
    "ext_go": lambda m: REPO / "src/mfp" / f"{m['n']}.py",
    "sidecar": lambda m: REPO / "asr" / m["n"],
    "api_go": lambda m: REPO / "src/mfp/server" / m["n"],
}

changed: list[str] = []
missing: list[str] = []

for zid, locate in LOCATE.items():
    for m in zones[zid]["members"]:
        if "lines" not in m:
            continue
        p = locate(m)
        if not p.is_file():
            missing.append(f"{zid}/{m['n']}: {p}")
            continue
        real = wc(p)
        if real != m["lines"]:
            changed.append(f"{m['n']}: {m['lines']} -> {real} ({real - m['lines']:+d})")
            m["lines"] = real

for m in zones["gui_go"]["members"]:
    real = wc_dir(m["dir"])
    if real < 0:
        missing.append(f"gui_go/{m['n']}: {REPO / m['dir']}")
        continue
    if real != m["lines"]:
        changed.append(f"{m['n']}: {m['lines']} -> {real} ({real - m['lines']:+d})")
        m["lines"] = real

# A subtitle that prints a total is a claim as much as a chip is, and it was
# the one nobody was checking until verify.py grew that assert. Rewrite the
# printed total in place rather than the whole subtitle, so the prose around
# it -- 「不在 mfp 的行程裡跑」 and the like -- survives a re-measure.
for z in M["zones"]:
    have = [m["lines"] for m in z["members"] if "lines" in m]
    if not have or "subtitle" not in z:
        continue
    total = sum(have)
    printed = [int(s.replace(",", "")) for s in re.findall(r"[\d,]*\d", z["subtitle"])]
    if total in printed:
        continue
    if len(printed) != 1:
        missing.append(
            f"{z['id']}: subtitle prints {printed}, cannot tell which is the total"
        )
        continue
    z["subtitle"] = re.sub(r"[\d,]*\d", f"{total:,}", z["subtitle"], count=1)
    changed.append(f"{z['id']} subtitle: {printed[0]:,} -> {total:,}")

if missing:
    print("STRUCTURAL CHANGE -- not a re-measure. Nothing written:")
    for line in missing:
        print(f"  {line}")
    sys.exit(1)

if not changed:
    print("every count already matches the repository; model.json untouched")
    sys.exit(0)

(HERE / "model.json").write_text(
    json.dumps(M, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"model.json re-measured -- {len(changed)} claim(s) moved:")
for line in changed:
    print(f"  {line}")
print("now run build.py, then verify.py")
