"""Three rungs, and the first one is the one that was missing.

The first version of this file compared `model.json` to the SVG built FROM
`model.json` -- one code judging itself. Calibration proved it: renaming a
module in the model produced a consistent picture and the checker passed.
So rung 0 now reads the REPOSITORY: `tests/unit/test_layering.py` decides
which module is core and which is an extension (it is the machine-enforced
answer, not a documented one), and `wc -l` decides every line count.

  rung 0  model  <-> repository   (is the diagram TRUE)
  rung 1  artifact <-> model      (is the picture COMPLETE)
  rung 2  artifact geometry       (is the picture SOUND)
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path(r"D:/AIWork/media-fetch-pipeline")
M = json.loads((HERE / "model.json").read_text(encoding="utf-8"))
HTML = (HERE / "index.html").read_text(encoding="utf-8")
BOXES = json.loads((HERE / "boxes.json").read_text(encoding="utf-8"))

fails: list[str] = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def html_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


zones = {z["id"]: z for z in M["zones"]}
names = {zid: [m["n"] for m in z["members"]] for zid, z in zones.items()}

# ============================================================ rung 0: truth
print("rung 0 -- model vs the repository (the diagram's CLAIMS)")

src = (REPO / "tests/unit/test_layering.py").read_text(encoding="utf-8")
tree = ast.parse(src)
sets: dict[str, set[str]] = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name) and tgt.id in ("CORE", "EXTENSIONS", "composers"):
            try:
                sets[tgt.id] = set(ast.literal_eval(node.value))
            except ValueError:
                pass

check({"CORE", "EXTENSIONS", "composers"} <= set(sets),
      f"read all three sets out of test_layering.py (got {sorted(sets)})")

check(set(names["core"]) == sets.get("CORE", set()),
      f"CORE matches the gate  (only-in-diagram={sorted(set(names['core']) - sets.get('CORE', set()))}, "
      f"only-in-gate={sorted(sets.get('CORE', set()) - set(names['core']))})")

drawn_ext = set(names["ext_stay"]) | set(names["ext_go"])
check(drawn_ext == sets.get("EXTENSIONS", set()),
      f"EXTENSIONS (stay + go) matches the gate  "
      f"(only-in-diagram={sorted(drawn_ext - sets.get('EXTENSIONS', set()))}, "
      f"only-in-gate={sorted(sets.get('EXTENSIONS', set()) - drawn_ext)})")

check(set(names["composers"]) == sets.get("composers", set()),
      "composers matches the gate")

check(not (set(names["ext_stay"]) & set(names["ext_go"])),
      "no extension is drawn on both sides of the seam")

# every line count the diagram prints must be the file's real length
def wc(p: pathlib.Path) -> int:
    return len(p.read_text(encoding="utf-8", errors="replace").splitlines())


def wc_dir(rel: str) -> int:
    """gui_go's members are DIRECTORIES. The rule that decides which files
    count is written into the zone (`count_rule`) instead of living only
    here, because an unstated rule is how 4,261 stood unchecked."""
    d = REPO / rel
    if not d.is_dir():
        return -1
    return sum(wc(p) for p in sorted(d.rglob("*"))
               if p.suffix in (".ts", ".tsx")
               and ".test." not in p.name and ".spec." not in p.name)


LOCATE = {
    "ext_go": lambda m: REPO / "src/mfp" / f"{m['n']}.py",
    "sidecar": lambda m: REPO / "asr" / m["n"],
    "api_go": lambda m: REPO / "src/mfp/server" / m["n"],
}
bad_counts = []
for zid, locate in LOCATE.items():
    for m in zones[zid]["members"]:
        if "lines" not in m:
            continue
        p = locate(m)
        real = wc(p) if p.is_file() else None
        if real != m["lines"]:
            bad_counts.append(f"{m['n']}: says {m['lines']}, file has {real}")
for m in zones["gui_go"]["members"]:
    real = wc_dir(m["dir"])
    if real != m["lines"]:
        bad_counts.append(f"{m['n']}: says {m['lines']}, {m['dir']} has {real}")
check(not bad_counts, f"every printed line count matches the file ({bad_counts})")

# A total printed in a zone SUBTITLE is a claim too -- and it was the one
# nobody was checking. Any digit group in a subtitle must be that zone's sum.
bad_totals = []
for z in M["zones"]:
    printed = [int(s.replace(",", "")) for s in re.findall(r"[\d,]*\d", z.get("subtitle", ""))]
    have = [m["lines"] for m in z["members"] if "lines" in m]
    if printed and have and sum(have) not in printed:
        bad_totals.append(f"{z['id']}: subtitle says {printed}, members sum to {sum(have)}")
check(not bad_totals, f"every zone subtitle total equals its members ({bad_totals})")

# the shared runtime the diagram points at must actually be there
missing_rt = [m["n"] for m in M["shared"]["members"]
              if not (pathlib.Path("D:/AIWork") / m["n"].replace("\\", "/")).exists()]
check(not missing_rt, f"the shared runtime paths exist on disk ({missing_rt})")

# ======================================================= rung 1: completeness
print("\nrung 1 -- artifact vs model (parsed out of index.html)")
svg_src = re.search(r"<svg .*?</svg>", HTML, re.S)
check(svg_src is not None, "the page contains one <svg> element")
root = ET.fromstring(svg_src.group(0))
ns = "{http://www.w3.org/2000/svg}"
texts = [(t.text or "").strip() for t in root.iter(f"{ns}text")]

expected = [m["n"] for z in M["zones"] for m in z["members"]]
missing = [n for n in expected if n not in texts]
check(not missing, f"every module name is rendered ({len(expected)} of them; missing: {missing})")
dupes = sorted({n for n in expected if texts.count(n) > 1})
check(not dupes, f"no module is drawn twice ({dupes})")
for z in M["zones"]:
    check(z["title"] in texts, f"zone title rendered: {z['title'][:24]}")
for c in M["callouts"]:
    check(c["title"] in texts, f"callout rendered: {c['title'][:24]}")
check(M["seam"]["criterion"] in texts, "the seam criterion is on the picture")
absent_rows = [r["item"] for r in M["status_rows"] if html_esc(r["item"]) not in HTML]
check(not absent_rows, f"every status row reached the table ({absent_rows})")

# =========================================================== rung 2: geometry
print("\nrung 2 -- geometry")
_, _, vw, vh = [float(v) for v in root.get("viewBox").split()]
outside = [b["label"] for b in BOXES
           if b["x"] < 0 or b["y"] < 0 or b["x"] + b["w"] > vw or b["y"] + b["h"] > vh]
check(not outside, f"every label box is inside the viewBox ({outside})")


def ov(a, b):
    return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


clashes = [(a["label"], b["label"])
           for i, a in enumerate(BOXES) for b in BOXES[i + 1:] if ov(a, b)]
check(not clashes, f"no two label boxes overlap ({len(clashes)}; first: {clashes[:2]})")

SEAM = 620.0
left = {b["label"] for b in BOXES if b["x"] + b["w"] <= SEAM}
right = {b["label"] for b in BOXES if b["x"] >= SEAM}
stay = {m["n"] for z in M["zones"] if z["side"] == "stay" for m in z["members"]}
go = {m["n"] for z in M["zones"] if z["side"] == "go" for m in z["members"]}
check(stay <= left, f"every STAY module is left of the seam ({sorted(stay - left)})")
check(go <= right, f"every GO module is right of the seam ({sorted(go - right)})")

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print(f"ALL GREEN — {len(expected)} modules, {len(BOXES)} boxes, "
      f"{len(M['status_rows'])} status rows.")
