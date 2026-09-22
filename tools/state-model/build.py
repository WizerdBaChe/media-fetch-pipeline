"""Render model.json into a self-contained page. The MODEL is canonical.

Every node, edge, coordinate and label in the output is computed from
`model.json`; nothing is typed into the SVG by hand. That is what makes the
picture re-derivable when the system changes -- edit the model, re-run this.

Geometry is grid-computed rather than hand-placed, so chip overlap is
impossible by construction; `verify.py` asserts it anyway, because "impossible
by construction" is a claim about the code and the gate has to read the
artifact.
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
M = json.loads((HERE / "model.json").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- geometry
W = 1240
MARGIN = 32
SEAM_X = 620
GUTTER = 24
LEFT_X, LEFT_W = MARGIN, SEAM_X - GUTTER - MARGIN            # 32 .. 596
RIGHT_X = SEAM_X + GUTTER                                     # 644
RIGHT_W = W - MARGIN - RIGHT_X                                # 564
PAD = 12
COLS = 4
CHIP_H, CHIP_GAP = 30, 8
CHIP_W = (LEFT_W - 2 * PAD - (COLS - 1) * CHIP_GAP) / COLS
HEAD_H = 38
PANEL_GAP = 16
TOP = 118


def panel_h(n: int) -> float:
    rows = max(1, -(-n // COLS))
    return HEAD_H + rows * (CHIP_H + CHIP_GAP) - CHIP_GAP + PAD


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


parts: list[str] = []
boxes: list[dict] = []          # every text-bearing rect, for verify.py


def chip(x, y, label, kind):
    boxes.append({"x": x, "y": y, "w": CHIP_W, "h": CHIP_H, "label": label})
    cls = f"chip chip-{kind}"
    parts.append(
        f'<g class="{cls}"><rect x="{x:.1f}" y="{y:.1f}" '
        f'width="{CHIP_W:.1f}" height="{CHIP_H}" rx="5"/>'
        f'<text x="{x + CHIP_W / 2:.1f}" y="{y + CHIP_H / 2 + 4.5:.1f}">'
        f"{esc(label)}</text></g>"
    )


def panel(x, w, y, zone):
    n = len(zone["members"])
    h = panel_h(n)
    side = zone["side"]
    parts.append(
        f'<g class="panel panel-{side}"><rect x="{x}" y="{y:.1f}" width="{w}" '
        f'height="{h:.1f}" rx="9"/>'
        f'<text class="p-title" x="{x + PAD}" y="{y + 20:.1f}">{esc(zone["title"])}</text>'
        f'<text class="p-sub" x="{x + PAD}" y="{y + 33:.1f}">{esc(zone["subtitle"])}</text></g>'
    )
    for i, m in enumerate(zone["members"]):
        cx = x + PAD + (i % COLS) * (CHIP_W + CHIP_GAP)
        cy = y + HEAD_H + (i // COLS) * (CHIP_H + CHIP_GAP)
        kind = m.get("mark") or side
        chip(cx, cy, m["n"], kind)
    return y + h + PANEL_GAP


zones = {z["id"]: z for z in M["zones"]}

y = TOP
for zid in ("core", "ext_stay", "composers"):
    y = panel(LEFT_X, LEFT_W, y, zones[zid])
left_bottom = y - PANEL_GAP

y = TOP
for zid in ("ext_go", "sidecar", "gui_go", "api_go"):
    y = panel(RIGHT_X, RIGHT_W, y, zones[zid])
right_bottom = y - PANEL_GAP

content_bottom = max(left_bottom, right_bottom)

# ------------------------------------------------------------------ seam
parts.insert(
    0,
    f'<line class="seam" x1="{SEAM_X}" y1="{TOP - 26}" x2="{SEAM_X}" '
    f'y2="{content_bottom + 10:.1f}"/>'
)
parts.append(
    f'<g class="seam-cap"><rect x="{SEAM_X - 116}" y="{TOP - 52}" width="232" '
    f'height="26" rx="13"/><text x="{SEAM_X}" y="{TOP - 34}">'
    f'{esc(M["seam"]["criterion"])}</text></g>'
)

# -------------------------------------------------------------- callouts
cy = content_bottom + 34
CO_W = (W - 2 * MARGIN - 24) / 2
CO_H = 126
for i, c in enumerate(M["callouts"]):
    cx = MARGIN + i * (CO_W + 24)
    parts.append(
        f'<g class="callout co-{c["kind"]}"><rect x="{cx:.1f}" y="{cy}" '
        f'width="{CO_W:.1f}" height="{CO_H}" rx="9"/>'
        f'<text class="co-t" x="{cx + 14:.1f}" y="{cy + 26}">{esc(c["title"])}</text>'
    )
    # wrap the body at ~30 CJK glyphs
    body, line, lines = c["body"], "", []
    for ch in body:
        line += ch
        if len(line) >= 30 and ch in "、。，）) ":
            lines.append(line)
            line = ""
    if line:
        lines.append(line)
    for j, ln in enumerate(lines[:5]):
        parts.append(
            f'<text class="co-b" x="{cx + 14:.1f}" y="{cy + 50 + j * 18}">'
            f"{esc(ln.strip())}</text>"
        )
    parts.append("</g>")
    boxes.append({"x": cx, "y": cy, "w": CO_W, "h": CO_H, "label": c["title"]})

# ---------------------------------------------------------------- shared
sy = cy + CO_H + 30
sh = M["shared"]
SH_H = 40 + len(sh["members"]) * 26 + 26
parts.append(
    f'<g class="shared"><rect x="{MARGIN}" y="{sy}" width="{W - 2 * MARGIN}" '
    f'height="{SH_H}" rx="9"/>'
    f'<text class="p-title" x="{MARGIN + PAD}" y="{sy + 22}">{esc(sh["title"])}</text>'
    f'<text class="p-sub" x="{MARGIN + PAD}" y="{sy + 38}">{esc(sh["note"])}</text>'
)
for i, m in enumerate(sh["members"]):
    ry = sy + 58 + i * 26
    parts.append(
        f'<text class="sh-n" x="{MARGIN + PAD}" y="{ry}">{esc(m["n"])}</text>'
        f'<text class="sh-d" x="{MARGIN + 420}" y="{ry}">{esc(m["detail"])}</text>'
        f'<text class="sh-s" x="{W - MARGIN - PAD}" y="{ry}">{esc(m["size"])}</text>'
    )
parts.append("</g>")

# ---------------------------------------------------------------- legend
ly = sy + SH_H + 28
LEG = [
    ("stay", "留在 mfp"),
    ("go", "移到 local-transcript-maker"),
    ("contested", "歸屬未定，抽出前要解決"),
    ("ruled", "已裁定留下（2026-09-08）"),
]
parts.append(f'<g class="legend">')
lx = MARGIN
for kind, text in LEG:
    parts.append(
        f'<rect class="chip chip-{kind}" x="{lx}" y="{ly}" width="26" height="16" rx="4"/>'
        f'<text class="lg" x="{lx + 34}" y="{ly + 13}">{esc(text)}</text>'
    )
    lx += 40 + len(text) * 15
parts.append("</g>")

H = ly + 44
svg = (
    f'<svg viewBox="0 0 {W} {H:.0f}" role="img" '
    f'aria-label="{esc(M["title"])}" xmlns="http://www.w3.org/2000/svg">'
    + "".join(parts)
    + "</svg>"
)

# ----------------------------------------------------------- status table
STATE = {
    "yes": ("ok", "成立"),
    "no": ("no", "尚未發生"),
    "partial": ("mid", "做了一半"),
    "attention": ("warn", "要處理"),
}
rows = "".join(
    f'<tr class="r-{STATE[r["state"]][0]}"><td class="s">'
    f'<span class="pill p-{STATE[r["state"]][0]}">{STATE[r["state"]][1]}</span></td>'
    f'<td class="i">{esc(r["item"])}</td><td class="e">{esc(r["evidence"])}</td></tr>'
    for r in M["status_rows"]
)

excl = "".join(
    f'<li><b>{esc(x["view"])}</b> — {esc(x["why"])}</li>' for x in M["coverage"]["excluded"]
)
srcs = "".join(
    f'<li><code>{esc(s["id"])}</code> {esc(s["what"])} <span class="role">{esc(s["role"])}</span></li>'
    for s in M["sources"]
)

html = f"""<html lang="zh-Hant" data-page-class="diagram"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(M["title"])}</title>
<style>
:root {{
  --ink:#1b1d21; --dim:#5b6068; --faint:#8a9099; --line:#d7dbe0;
  --bg:#fbfbfc; --card:#ffffff;
  --stay:#e8f1ea; --stay-b:#3f7d54; --go:#fdeee4; --go-b:#c2622a;
  --hot:#fdf3d8; --hot-b:#a4832a; --rule:#e6eefb; --rule-b:#3a6ea8;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.65 "Microsoft JhengHei","PingFang TC","Noto Sans TC",system-ui,sans-serif; }}
main {{ width:min(1560px, 94vw); margin:0 auto; padding:28px 0 64px; }}
h1 {{ font-size:25px; margin:0 0 4px; letter-spacing:.2px; }}
.sub {{ color:var(--dim); margin:0 0 6px; }}
.stamp {{ color:var(--faint); font-size:13px; margin:0 0 22px; }}
.stamp code {{ background:#eef0f3; padding:1px 5px; border-radius:4px; }}
.figure {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:14px; margin-bottom:30px; }}
svg {{ display:block; width:100%; height:auto; }}
.panel rect {{ fill:#fff; stroke:var(--line); }}
.panel-stay rect {{ fill:#f7faf8; stroke:#c3d8c9; }}
.panel-go rect {{ fill:#fdf8f4; stroke:#e8cdb6; }}
.p-title {{ font:600 14px "Microsoft JhengHei",sans-serif; fill:var(--ink); }}
.p-sub {{ font:12px "Microsoft JhengHei",sans-serif; fill:var(--dim); }}
.chip rect, rect.chip {{ stroke-width:1.4; }}
.chip-stay rect, rect.chip-stay {{ fill:var(--stay); stroke:var(--stay-b); }}
.chip-go rect, rect.chip-go {{ fill:var(--go); stroke:var(--go-b); stroke-dasharray:5 3; }}
.chip-contested rect, rect.chip-contested {{ fill:var(--hot); stroke:var(--hot-b); stroke-width:2.2; stroke-dasharray:2 2; }}
.chip-ruled rect, rect.chip-ruled {{ fill:var(--rule); stroke:var(--rule-b); stroke-width:2; }}
.chip text {{ font:12.5px "Consolas","Microsoft JhengHei",monospace; fill:var(--ink); text-anchor:middle; }}
.seam {{ stroke:#9aa1aa; stroke-width:2.5; stroke-dasharray:9 6; }}
.seam-cap rect {{ fill:#eef0f3; stroke:#c4c9d0; }}
.seam-cap text {{ font:600 12px "Microsoft JhengHei",sans-serif; fill:var(--ink); text-anchor:middle; }}
.callout rect {{ fill:#fffdf6; stroke:var(--hot-b); stroke-width:1.6; }}
.co-ruled rect {{ fill:#f6f9fe; stroke:var(--rule-b); }}
.co-t {{ font:600 13.5px "Microsoft JhengHei",sans-serif; fill:var(--ink); }}
.co-b {{ font:12.5px "Microsoft JhengHei",sans-serif; fill:var(--dim); }}
.shared rect {{ fill:#f4f5f7; stroke:#c4c9d0; stroke-width:1.4; }}
.sh-n {{ font:12.5px "Consolas",monospace; fill:var(--ink); }}
.sh-d {{ font:12.5px "Microsoft JhengHei",sans-serif; fill:var(--dim); }}
.sh-s {{ font:600 12.5px "Consolas",monospace; fill:var(--ink); text-anchor:end; }}
.lg {{ font:12.5px "Microsoft JhengHei",sans-serif; fill:var(--dim); }}
h2 {{ font-size:19px; margin:34px 0 10px; }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
th, td {{ text-align:left; padding:10px 14px; border-bottom:1px solid #eceef1;
  vertical-align:top; font-size:14px; }}
th {{ background:#f3f5f7; font-weight:600; font-size:13px; color:var(--dim); }}
tr:last-child td {{ border-bottom:none; }}
td.s {{ width:96px; }} td.i {{ width:31%; font-weight:600; }}
td.e {{ color:var(--dim); font-size:13.5px; }}
.pill {{ display:inline-block; padding:2px 9px; border-radius:11px;
  font-size:12px; font-weight:600; white-space:nowrap; }}
.p-ok {{ background:var(--stay); color:#2c5c3d; }}
.p-no {{ background:#eceef1; color:#5b6068; }}
.p-mid {{ background:var(--rule); color:#2b5580; }}
.p-warn {{ background:var(--hot); color:#7d6218; }}
.notes {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-top:30px; }}
.note {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 20px; }}
.note h3 {{ margin:0 0 8px; font-size:15px; }}
.note ul {{ margin:0; padding-left:20px; color:var(--dim); font-size:13.5px; }}
.note li {{ margin-bottom:7px; }}
.role {{ color:var(--faint); }}
code {{ font-family:"Consolas",monospace; }}
@media (max-width:900px) {{ .notes {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>{esc(M["title"])}</h1>
<p class="sub">{esc(M["question"])}</p>
<p class="stamp">狀態日期 {esc(M["as_of"])}　·　HEAD <code>{esc(M["head"])}</code>　·
每一格都可以從 <code>model.json</code> 重算，畫面是它的投影</p>

<div class="figure">{svg}</div>

<h2>現在的真實狀況</h2>
<table><thead><tr><th>判定</th><th>項目</th><th>證據</th></tr></thead>
<tbody>{rows}</tbody></table>

<div class="notes">
<div class="note"><h3>這張圖沒有畫的，以及為什麼</h3><ul>{excl}</ul></div>
<div class="note"><h3>每一格的來源</h3><ul>{srcs}</ul></div>
</div>
</main></body></html>"""

out = HERE / "index.html"
out.write_text(html, encoding="utf-8")
(HERE / "boxes.json").write_text(json.dumps(boxes, ensure_ascii=False), encoding="utf-8")
print(f"wrote {out}  ({len(html)} bytes)  svg viewBox 0 0 {W} {H:.0f}")
print(f"chips+callouts placed: {len(boxes)}")
