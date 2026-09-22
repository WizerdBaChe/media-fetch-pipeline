"""Does the owner view tell the truth the canonical model tells?

The division of labour matters. `verify.py` rung 0 already checks
model.json against the REPOSITORY, and it is green; this file therefore
takes model.json as its ground truth and asks a different question:

  rung 0  owner model <-> canonical model   (nothing invented, nothing
          silently dropped, no number typed by hand)
  rung 1  artifact <-> owner model          (is the page COMPLETE)
  rung 2  geometry                          (is the picture SOUND -- and
          this one includes edge-through-box, which is what a node-and-edge
          diagram can get wrong that a chip grid cannot)
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
M = json.loads((HERE / "model.json").read_text(encoding="utf-8"))
O = json.loads((HERE / "owner-model.json").read_text(encoding="utf-8"))
HTML = (HERE / "index-owner-view.html").read_text(encoding="utf-8")

fails: list[str] = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def digits(s: str) -> set[str]:
    return set(re.findall(r"[\d,]*\d", s))


NODES = {n["id"]: n for n in O["nodes"]}
Z = {z["id"]: z for z in M["zones"]}

# ================================================ rung 0: owner vs canonical
print("rung 0 -- owner view vs the canonical model")

canon_members = {m["n"]: z["id"] for z in M["zones"] for m in z["members"]}
canon_members.update({m["n"]: "shared" for m in M["shared"]["members"]})

traced = [n for v in O["trace"].values() for n in v]
cut = set(O["cut"])

check(len(traced) == len(set(traced)),
      f"no module is traced to two boxes ({[n for n in traced if traced.count(n) > 1]})")
check(not (set(traced) & cut), f"nothing is both drawn and cut ({sorted(set(traced) & cut)})")

orphans = sorted(set(canon_members) - set(traced) - cut)
check(not orphans, f"every canonical module is either drawn or named as cut ({orphans})")
invented = sorted((set(traced) | cut) - set(canon_members))
check(not invented, f"nothing exists here that the canonical model does not have ({invented})")
check(set(O["trace"]) == set(NODES),
      f"every box has a trace row ({sorted(set(NODES) ^ set(O['trace']))})")

# a box's side must agree with the canonical side of everything inside it
SIDE = {z["id"]: z["side"] for z in M["zones"]}
SIDE["shared"] = "shared"
bad_side = []
for nid, members in O["trace"].items():
    want = {SIDE[canon_members[m]] for m in members}
    if want != {NODES[nid]["side"]}:
        bad_side.append(f"{nid} drawn as {NODES[nid]['side']} but contains {sorted(want)}")
check(not bad_side, f"no box straddles the seam ({bad_side})")

# every number on the page, recomputed from the canonical model
def zl(zid):
    return sum(m["lines"] for m in Z[zid]["members"] if "lines" in m)


go = [z["id"] for z in M["zones"] if z["side"] == "go"]
want_num = {
    "go_count": f"{sum(len(Z[z]['members']) for z in go)}",
    "go_lines": f"{sum(zl(z) for z in go):,}",
    "shared_gb": f"{sum(float(m['size'].split()[0]) / (1024 if m['size'].endswith('MB') else 1) for m in M['shared']['members']):.1f}",
    "unsettled": f"{len([r for r in M['status_rows'] if r['state'] != 'yes'])}",
    **{z: f"{zl(z):,}" for z in go},
}
check(want_num == O["numbers"],
      f"every published number recomputes from the model "
      f"(page={O['numbers']}, recomputed={want_num})")

# =================================================== rung 1: page vs the model
print("\nrung 1 -- the page vs the owner model")
svg_src = re.search(r"<svg .*?</svg>", HTML, re.S)
check(svg_src is not None, "the page contains one <svg>")
root = ET.fromstring(svg_src.group(0))
ns = "{http://www.w3.org/2000/svg}"
svg_text = [(t.text or "").strip() for t in root.iter(f"{ns}text")]

miss = [n["name"] for n in O["nodes"] if n["name"] not in svg_text]
check(not miss, f"every box label is drawn ({len(O['nodes'])} boxes; missing {miss})")
miss = [n["role"] for n in O["nodes"] if n["role"] not in svg_text]
check(not miss, f"every box's plain-language role is drawn ({miss})")
check(len([p for p in root.iter(f"{ns}path") if p.get("class", "").startswith("edge")])
      == len(O["edges"]),
      f"every connector is drawn ({len(O['edges'])})")
labelled = [e["label"] for e in O["edges"] if e["label"]]
check(all(l in svg_text for l in labelled), f"every connector label is drawn ({labelled})")

body = html.unescape(
    re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>|<!--.*?-->", " ", HTML, flags=re.S)))
miss = [n["name"] for n in O["nodes"] if n["name"] not in body]
check(not miss, f"the mapping table has a row per box ({miss})")
miss = [c for c in O["cut"] if c not in body]
check(not miss, f"every cut module is named in the page ({miss})")
anchors = {html.unescape(a) for a in re.findall(r'<tr [^>]*data-canon="([^"]*)"', HTML)}
want = {r["item"] for r in M["status_rows"]}
check(anchors == want,
      f"every canonical status row survived into the reader's table, none invented "
      f"(missing={sorted(want - anchors)}, extra={sorted(anchors - want)})")
miss = [r["evidence"] for r in M["status_rows"] if r["evidence"] not in body]
check(not miss, f"every row's EVIDENCE is verbatim -- reworded evidence is not evidence ({miss})")
miss = [c["title"] for c in M["callouts"] if c["title"] not in body]
check(not miss, f"both canonical callouts survived aggregation ({miss})")

# nothing was typed: every number visible on the page must come from the
# canonical model, from the computed set, or be a marker id
allowed = digits((HERE / "model.json").read_text(encoding="utf-8"))
allowed |= set(O["numbers"].values())
allowed |= digits(re.search(r"<code>([0-9a-f]{12})</code>", HTML).group(1))
allowed |= {"1", "2"}
stray = sorted(digits(body) - allowed)
check(not stray, f"no number on the page was typed by hand ({stray})")

# =========================================================== rung 2: geometry
print("\nrung 2 -- geometry")
VW, VH = O["viewbox"]
_, _, vw, vh = [float(v) for v in root.get("viewBox").split()]
check((vw, vh) == (VW, VH), f"the drawn viewBox is the model's ({vw}x{vh})")

boxes = O["nodes"]
out = [b["id"] for b in boxes
       if b["x"] < 0 or b["y"] < 0 or b["x"] + b["w"] > vw or b["y"] + b["h"] > vh]
check(not out, f"every box is inside the viewBox ({out})")


def ov(a, b, pad=0.0):
    return not (a["x"] + a["w"] + pad <= b["x"] or b["x"] + b["w"] + pad <= a["x"]
                or a["y"] + a["h"] + pad <= b["y"] or b["y"] + b["h"] + pad <= a["y"])


clash = [(a["id"], b["id"]) for i, a in enumerate(boxes) for b in boxes[i + 1:] if ov(a, b, 12)]
check(not clash, f"no two boxes touch or overlap (12px gutter) ({clash})")

SEAM = O["seam"]
wrong = [b["id"] for b in boxes if b["side"] == "stay" and b["x"] + b["w"] > SEAM]
wrong += [b["id"] for b in boxes if b["side"] == "go" and b["x"] < SEAM]
check(not wrong, f"every box is on its own side of the seam ({wrong})")

# --- a connector may not run through a box that is not one of its endpoints
def segs(d: str):
    pts = [tuple(float(v) for v in p.split(",")) for p in re.findall(r"[ML]([-\d.]+,[-\d.]+)", d)]
    return list(zip(pts, pts[1:]))


def hits(p1, p2, b, pad=4.0):
    """axis-aligned segment vs rect; every path in this diagram is orthogonal"""
    x0, x1 = sorted((p1[0], p2[0]))
    y0, y1 = sorted((p1[1], p2[1]))
    return not (x1 <= b["x"] + pad or b["x"] + b["w"] - pad <= x0
                or y1 <= b["y"] + pad or b["y"] + b["h"] - pad <= y0)


paths = [p for p in root.iter(f"{ns}path") if p.get("class", "").startswith("edge")]
check(len(paths) == len(O["edges"]), "one drawn path per modelled connector")
pierced = []
for e, p in zip(O["edges"], paths):
    for s in segs(p.get("d")):
        for b in boxes:
            if b["id"] in (e["a"], e["b"]):
                continue
            if hits(*s, b):
                pierced.append(f"{e['a']}->{e['b']} crosses {b['id']}")
check(not pierced, f"no connector runs through an unrelated box ({sorted(set(pierced))})")

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print(f"ALL GREEN — {len(boxes)} boxes, {len(O['edges'])} connectors, "
      f"{len(traced)} modules traced, {len(cut)} named as cut.")
