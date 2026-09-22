"""A1 owner view — the same KIND of artifact as index.html, redrawn for the
person who decides how the work is split rather than the person who writes it.

Canonical stays untouched. Two properties this script must not lose:

  * every box traces to named members of model.json (TRACE), and every
    member not in a box is named in CUT with a reason. TRACE + CUT is a
    partition of the canonical model -- verify_owner.py rung 0 asserts it,
    so an invented box or a silently dropped module cannot ship.
  * every number on the page is COMPUTED from model.json here, never typed.
    A typed number is how "2,775 行" nearly went out.

R11: coordinates derive from the anchor block. Editing one moves a COLUMN,
not a box.
"""

from __future__ import annotations

import hashlib
import json
import pathlib


HERE = pathlib.Path(__file__).resolve().parent
M = json.loads((HERE / "model.json").read_text(encoding="utf-8"))
Z = {z["id"]: z for z in M["zones"]}

# ============================================================== anchors (R11)
W, H = 1340, 742
COL = {"c1": 140, "c2": 400, "c3": 660, "c4": 920, "c5": 1180}
ROW = {"r1": 180, "r2": 320, "r3": 460, "r4": 600}
NW, NH = 200, 76
SEAM = (COL["c3"] + COL["c4"]) / 2      # 790 — 30px clear of both columns
FEED_Y = 250                            # agent corridor, between rows 1 and 2
RETURN_Y = 690                          # write-back corridor, below row 4

# id -> (col, row, side, label, role)
NODES = [
    ("ui_dl",  "c1", "r1", "stay",   "操作介面",        "命令列與視窗，人從這裡開始"),
    ("agent",  "c3", "r1", "stay",   "代理人介面",      "讓 AI 助理直接驅動這個工具"),
    ("ui_tr",  "c4", "r1", "go",     "逐字稿工作區",    "人在這裡讀稿、改稿"),
    ("url",    "c1", "r2", "stay",   "一個公開連結",    "整件事的入口"),
    ("fetch",  "c2", "r2", "stay",   "取得素材",        "談速率、抓影片與圖片"),
    ("subs",   "c3", "r2", "stay",   "平台自帶字幕",    "平台已經有的就直接拿"),
    ("stack",  "c1", "r3", "stay",   "長圖拼接",        "把連續截圖接成一張"),
    ("store",  "c2", "r3", "stay",   "檔案落地與紀錄",  "每個檔去了哪裡、誰要它存在"),
    ("asr",    "c4", "r3", "go",     "聽寫",            "把聲音變成有時間的文字"),
    ("judge",  "c5", "r3", "go",     "品質判定",        "指出稿子哪裡不可信"),
    ("brief",  "c1", "r4", "stay",   "圖片影片解說",    "只開放給代理人，不進畫面"),
    ("engine", "c4", "r4", "shared", "語音引擎與模型",  "兩個產品共用的同一份"),
    ("fix",    "c5", "r4", "go",     "清理、校正與翻譯", "去重複、補術語、換語言"),
]
N = {n[0]: n for n in NODES}

# ---------------------------------------------------------- R5 traceability
# box -> the canonical members it stands for.  Nothing else may be drawn.
TRACE = {
    "ui_dl":  ["cli", "serve"],
    "agent":  ["agent", "capabilities"],
    "ui_tr":  ["transcript-workspace", "read-transcript", "refine-transcript",
               "translate-transcript", "translate-document", "setup-asr"],
    "url":    ["inputs", "policy", "redact"],
    "fetch":  ["download", "budget", "pipeline", "toolchain", "mediatool"],
    "subs":   ["captions"],
    "store":  ["runs", "index", "naming", "winpath", "logs"],
    "stack":  ["stack"],
    "brief":  ["brief"],
    "asr":    ["asr", "asr_models", "transcript", "runs_transcript",
               "runner.py", "translate_runner.py", "fetch_model.py"],
    "judge":  ["quality", "cues"],
    "fix":    ["correct", "tidy", "refine", "translate", "translate_doc"],
    "engine": [m["n"] for m in M["shared"]["members"]],
}

# member -> why it is NOT on the picture (R12 cut order: 3 = merged away,
# 4 = out of the question this view answers)
CUT = {
    "config": "設定與錯誤型別，每一格都會用到；畫出來只會變成連到所有東西的線",
    "errors": "同上",
    "models": "同上",
    "doctor": "診斷指令，不在主流程上",
    "queue": "排程與行程管理，屬於「怎麼跑」而不是「做什麼」",
    "capture": "同上",
    "sidecar": "同上",
    "routes_transcript.py": "服務層路由，是介面到功能之間的接線，不是獨立的一件事",
    "routes_asr.py": "同上",
}

EDGES = [
    ("ui_dl", "b", "url", "t", "line", None, (0, 0)),
    ("url", "r", "fetch", "l", "line", None, (0, 0)),
    ("fetch", "r", "subs", "l", "line", None, (0, 0)),
    ("fetch", "b", "store", "t", "line", None, (-30, -30)),
    ("subs", "b", "store", "t", "vstep", None, (0, 30)),
    ("agent", "b", "fetch", "t", "feed", None, (0, 60)),
    ("store", "l", "stack", "r", "line", None, (0, 0)),
    ("store", "b", "brief", "t", "hstep", None, (0, 0)),
    ("store", "r", "asr", "l", "line", "音檔", (0, 0)),
    ("ui_tr", "b", "asr", "t", "line", None, (0, 0)),
    ("asr", "r", "judge", "l", "line", None, (0, 0)),
    ("judge", "b", "fix", "t", "line", None, (0, 0)),
    ("engine", "t", "asr", "b", "line", None, (0, 0)),
    ("engine", "r", "fix", "l", "dash", None, (0, 0)),
    ("fix", "b", "store", "b", "return", "成品寫回", (0, 0)),
]

MARKS = {                                # anchored on the canonical callouts
    "store": ("1", "runs"),
    "subs": ("2", "captions"),
}

# ============================================================ computed facts
def zone_lines(zid):
    return sum(m["lines"] for m in Z[zid]["members"] if "lines" in m)


GO_ZONES = [z["id"] for z in M["zones"] if z["side"] == "go"]
GO_COUNT = sum(len(Z[z]["members"]) for z in GO_ZONES)
GO_LINES = sum(zone_lines(z) for z in GO_ZONES)


def gb(s):
    v, u = s.split()
    return float(v) / 1024 if u == "MB" else float(v)


SHARED_GB = sum(gb(m["size"]) for m in M["shared"]["members"])
UNSETTLED = [r for r in M["status_rows"] if r["state"] in ("attention", "no", "partial")]

NUM = {                                  # every number allowed on the page
    "go_count": f"{GO_COUNT}",
    "go_lines": f"{GO_LINES:,}",
    "shared_gb": f"{SHARED_GB:.1f}",
    "unsettled": f"{len(UNSETTLED)}",
    **{z: f"{zone_lines(z):,}" for z in GO_ZONES},
}


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cx(i): return COL[N[i][1]]
def cy(i): return ROW[N[i][2]]


def port(i, side, off=0.0):
    x, y = cx(i), cy(i)
    return {"l": (x - NW / 2, y + off), "r": (x + NW / 2, y + off),
            "t": (x + off, y - NH / 2), "b": (x + off, y + NH / 2)}[side]


# ==================================================================== render
svg = [f"<!-- cols {COL} rows {ROW} node {NW}x{NH} seam {SEAM} -->"]
svg.append(f'<line class="seam" x1="{SEAM}" y1="100" x2="{SEAM}" y2="{RETURN_Y + 14}"/>')
svg.append(f'<text class="sl" x="{SEAM - 14}" y="92" text-anchor="end">← 留在下載工具</text>')
svg.append(f'<text class="sr" x="{SEAM + 14}" y="92">搬去新的逐字稿工具 →</text>')

for a, pa, b, pb, kind, label, (oa, ob) in EDGES:
    x1, y1 = port(a, pa, oa)
    x2, y2 = port(b, pb, ob)
    cls = "edge dash" if kind == "dash" else "edge"
    if kind in ("line", "dash"):
        d = f"M{x1},{y1} L{x2},{y2}"
    elif kind == "vstep":
        d = f"M{x1},{y1} L{x1},{y1 + 40} L{x2},{y1 + 40} L{x2},{y2}"
    elif kind == "hstep":
        d = f"M{x1},{y1} L{x1},{y1 + 32} L{x2},{y1 + 32} L{x2},{y2}"
    elif kind == "feed":
        d = f"M{x1},{y1} L{x1},{FEED_Y} L{x2},{FEED_Y} L{x2},{y2}"
    else:                                # return corridor, under everything
        d = (f"M{x1},{y1} L{x1},{RETURN_Y} L{x2},{RETURN_Y} L{x2},{y2}")
    svg.append(f'<path class="{cls}" d="{d}" marker-end="url(#ar)"/>')
    if label:
        lx, ly = ((x1 + x2) / 2, RETURN_Y - 9) if kind == "return" else ((x1 + x2) / 2, y1 - 9)
        svg.append(f'<text class="el" x="{lx}" y="{ly}">{esc(label)}</text>')

for nid, c, r, side, name, role in NODES:
    x, y = COL[c] - NW / 2, ROW[r] - NH / 2
    svg.append(
        f'<g class="node n-{side}"><rect x="{x}" y="{y}" width="{NW}" height="{NH}" rx="8"/>'
        f'<text class="nn" x="{COL[c]}" y="{ROW[r] - 6}">{esc(name)}</text>'
        f'<text class="nr" x="{COL[c]}" y="{ROW[r] + 18}">{esc(role)}</text></g>'
    )
    if nid in MARKS:
        svg.append(f'<g class="mk"><circle cx="{x + NW - 14}" cy="{y + 14}" r="10"/>'
                   f'<text x="{x + NW - 14}" y="{y + 18.5}">{MARKS[nid][0]}</text></g>')

LEG = [("n-stay", "留在下載工具"), ("n-go", "搬去逐字稿工具"), ("n-shared", "兩邊共用")]
lx = 40
for i, (cls, txt) in enumerate(LEG):
    svg.append(f'<g class="node {cls}"><rect x="{lx}" y="52" width="22" height="18" rx="4"/></g>'
               f'<text class="lg" x="{lx + 30}" y="66">{esc(txt)}</text>')
    lx += 200

svg_el = (f'<svg viewBox="0 0 {W} {H}" role="img" '
          f'aria-label="下載工具與逐字稿工具的分界" xmlns="http://www.w3.org/2000/svg">'
          f'<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
          f'markerHeight="7" orient="auto-start-reverse">'
          f'<path d="M0,0 L10,5 L0,10 z" fill="#6b7280"/></marker></defs>'
          + "".join(svg) + "</svg>")

# ===================================================================== prose
DECIDE = [
    ("現在該怎麼分工",
     "兩件事各自成案：下載工具這邊把還欠的收尾，逐字稿工具那邊從頭開一個專案。"
     "今天卡住的不是技術，是兩件事擠在同一個資料夾裡，同一天有兩條線在改它。"),
    ("搬家有多大",
     f"要走的是 {NUM['go_count']} 個模組、約 {NUM['go_lines']} 行——"
     f"程式 {NUM['ext_go']} 行、獨立引擎 {NUM['sidecar']} 行、"
     f"畫面 {NUM['gui_go']} 行、接線 {NUM['api_go']} 行。這不是一次改版，是一次分家。"),
    ("動工前一定要先解決的",
     "「檔案落地與紀錄」的歸屬（圖上的 ①）。它是兩邊唯一真正黏在一起的地方，"
     "而且自動檢查看不見這件事——機器只看誰呼叫誰，看不見「這個檔案內容整份是逐字稿的形狀」。"),
    ("現在能不能用",
     f"能。聽寫、翻譯、下載都實測可用；引擎與模型（共 {NUM['shared_gb']} GB）"
     f"已經搬到中性位置，舊的原型資料夾可以刪。但還有 {NUM['unsettled']} 件事沒收尾，列在最後。"),
]

C = {c["anchor"]: c for c in M["callouts"]}
notes = [
    ("1", "檔案落地與紀錄", C["runs"]["title"],
     "自動檢查判它屬於下載工具，但它的內部形狀整個是逐字稿的，"
     "而要走的每一個模組都靠它存檔。抽出之前這一條必須先解決。"),
    ("2", "平台自帶字幕", C["captions"]["title"],
     "拿平台現成的字幕是「取得」，不是「理解」，所以它留下。"
     "旁邊的「品質判定」沒有跟著留——判斷一句話是不是可信，那是理解。"),
]
notes_html = "".join(
    f'<li><span class="b">{n}</span><b>{esc(name)}</b>　{esc(t)}<br><span class="w">{esc(body)}</span></li>'
    for n, name, t, body in notes)

state = {"yes": ("ok", "可用"), "no": ("no", "還沒有"),
         "partial": ("mid", "一半"), "attention": ("mid", "要注意")}

# The reader gets plain wording; the canonical phrasing rides along as an
# anchor so verify_owner.py can still prove no row was dropped or invented.
# The EVIDENCE column is never reworded -- that is what makes it evidence.
ROW_LABEL = {
    "逐字稿家族已離開 mfp": "逐字稿功能已經搬出去",
    "local-transcript-maker repo 已建立": "新產品的專案已經開好",
    "驗證硬閘（全梯第三階）": "設計的正式檢查（動工前的關卡）",
    "mfp 本體可用": "下載工具本體可用",
    "git 乾淨": "版本紀錄乾淨",
    "release 是當前的": "可安裝的版本是最新的",
    "release/ 有殘留": "安裝檔資料夾有舊檔殘留",
    "舊原型可刪": "舊的原型資料夾可以刪",
    "runs.py 的歸屬": "「檔案落地與紀錄」該歸哪一邊",
    "vadSeconds > audioSeconds": "聽寫的健康數字算錯了",
    "Z15 欠的人工驗收": "字幕抓取還欠一次人工驗收",
}
rows = "".join(
    f'<tr class="s-{state[r["state"]][0]}" data-canon="{esc(r["item"])}">'
    f'<td>{esc(ROW_LABEL.get(r["item"], r["item"]))}</td>'
    f'<td><span class="pill">{state[r["state"]][1]}</span></td>'
    f'<td>{esc(r["evidence"])}</td></tr>' for r in M["status_rows"])

maprows = "".join(
    f'<tr><td>{esc(N[k][4])}</td><td>{esc("、".join(v))}</td></tr>'
    for k, v in TRACE.items())
cutrows = "".join(
    f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in CUT.items())

canon_sha = hashlib.sha256((HERE / "index.html").read_bytes()).hexdigest()[:12]

html = f"""<html lang="zh-Hant" data-page-class="diagram"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>下載工具與逐字稿工具的分界 — 負責人視角</title>
<style>
:root{{--ink:#1b1d21;--dim:#585e66;--faint:#8a9099;--line:#d9dde2;--bg:#fbfbfc;--card:#fff;
--stay:#e7f0ea;--stayb:#3f7d54;--go:#fdeee3;--gob:#c2622a;--sh:#eceef1;--shb:#7d848d;
--hot:#fdf3d8;--hotb:#a4832a;--okb:#3f7d54;--nob:#b4483c;--midb:#a4832a}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.72 "Microsoft JhengHei","PingFang TC",system-ui,sans-serif}}
main{{width:min(1580px,95vw);margin:0 auto;padding:26px 0 60px}}
h1{{font-size:25px;margin:0 0 6px}} h2{{font-size:19px;margin:34px 0 12px}}
.lead{{color:var(--dim);margin:0;font-size:16px}}
.prov{{background:#f1f3f5;border-left:3px solid var(--shb);padding:11px 16px;margin:16px 0 22px;
font-size:13px;color:var(--dim);border-radius:0 6px 6px 0;line-height:1.85}}
.prov code{{background:#e3e6e9;padding:1px 5px;border-radius:4px;font-size:12px}}
.figure{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
svg{{display:block;width:100%;height:auto}}
.node rect{{stroke-width:1.6}}
.n-stay rect{{fill:var(--stay);stroke:var(--stayb)}}
.n-go rect{{fill:var(--go);stroke:var(--gob);stroke-dasharray:6 4}}
.n-shared rect{{fill:var(--sh);stroke:var(--shb);stroke-dasharray:2 3}}
.nn{{font:600 15px "Microsoft JhengHei",sans-serif;fill:var(--ink);text-anchor:middle}}
.nr{{font:12.5px "Microsoft JhengHei",sans-serif;fill:var(--dim);text-anchor:middle}}
.lg{{font:12.5px "Microsoft JhengHei",sans-serif;fill:var(--dim)}}
.edge{{fill:none;stroke:#6b7280;stroke-width:1.8}} .edge.dash{{stroke-dasharray:6 5}}
.el{{font:12px "Microsoft JhengHei",sans-serif;fill:var(--dim);text-anchor:middle}}
.seam{{stroke:#9aa1aa;stroke-width:2.5;stroke-dasharray:9 6}}
.sl{{font:600 13px "Microsoft JhengHei",sans-serif;fill:var(--stayb)}}
.sr{{font:600 13px "Microsoft JhengHei",sans-serif;fill:var(--gob)}}
.mk circle{{fill:var(--hot);stroke:var(--hotb);stroke-width:1.6}}
.mk text{{font:600 12px sans-serif;fill:#7d6218;text-anchor:middle}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:18px}}
.d{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:15px 19px}}
.d h3{{margin:0 0 6px;font-size:15px}} .d p{{margin:0;color:var(--dim);font-size:14px}}
ul.notes{{list-style:none;margin:20px 0 0;padding:15px 20px;background:#fffdf6;
border:1px solid var(--hotb);border-radius:10px;font-size:14px}}
ul.notes li{{margin:9px 0;padding-left:30px;position:relative}}
ul.notes .b{{position:absolute;left:0;top:1px;width:20px;height:20px;border-radius:50%;
background:var(--hot);border:1.5px solid var(--hotb);color:#7d6218;font:600 12px sans-serif;
text-align:center;line-height:17px}}
ul.notes .w{{color:var(--dim)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13.5px}}
th,td{{text-align:left;padding:9px 14px;border-bottom:1px solid #eef0f2;vertical-align:top}}
th{{background:#f2f4f6;font-size:12.5px;color:var(--dim);font-weight:600}}
tr:last-child td{{border-bottom:none}} td:first-child{{font-weight:600;width:22%}}
.pill{{display:inline-block;padding:1px 9px;border-radius:11px;font-size:12px;white-space:nowrap;
border:1px solid currentColor}}
tr.s-ok .pill{{color:var(--okb)}} tr.s-no .pill{{color:var(--nob)}} tr.s-mid .pill{{color:var(--midb)}}
tr.s-ok td:nth-child(2),tr.s-no td:nth-child(2),tr.s-mid td:nth-child(2){{width:82px}}
tr td:last-child{{color:var(--dim)}}
.two{{display:grid;grid-template-columns:1.15fr 1fr;gap:20px;align-items:start}}
@media (max-width:1000px){{.two{{grid-template-columns:1fr}}}}
.foot{{margin-top:20px;color:var(--faint);font-size:12.5px;line-height:1.8}}
</style></head><body><main>
<h1>下載工具與逐字稿工具的分界</h1>
<p class="lead">一張圖回答一件事：這個產品現在有幾塊、哪幾塊要搬走、搬之前卡在哪裡。</p>

<div class="prov">
這是工程版 <code>index.html</code> 的<b>負責人視角版本</b>，不是它的取代品。
工程版有完整的模組檔名、行數與每一格的證據來源；這一份把它們收成能做決定的粒度，
沒有新增任何工程版沒有的事實。<br>
狀態日期 <b>{esc(M["as_of"])}</b>　·　對應版本 <code>{esc(M["head"])}</code>　·　工程版指紋
<code>{canon_sha}</code>　·　<b>工程版重新產生時這一份要跟著重畫</b>——否則它會安靜地說謊。
</div>

<div class="figure">{svg_el}</div>
<ul class="notes">{notes_html}</ul>

<h2>這張圖對管理的四句話</h2>
<div class="grid2">{"".join(f'<div class="d"><h3>{esc(a)}</h3><p>{esc(b)}</p></div>' for a, b in DECIDE)}</div>

<h2>現在的真實狀況</h2>
<table><thead><tr><th>項目</th><th>狀態</th><th>憑什麼這樣說</th></tr></thead><tbody>{rows}</tbody></table>

<h2>每一格背後是什麼，以及沒畫進去的</h2>
<div class="two">
<table><thead><tr><th>圖上的格子</th><th>實際包含（工程版的名字）</th></tr></thead>
<tbody>{maprows}</tbody></table>
<table><thead><tr><th>沒畫進圖裡</th><th>為什麼</th></tr></thead>
<tbody>{cutrows}</tbody></table>
</div>
<p class="foot">
左表加右表就是工程版的全部——沒有一個模組憑空消失，也沒有一個方塊是憑空多出來的；
這件事由 <code>verify_owner.py</code> 每次重畫時強制檢查。<br>
這一份<b>不帶完整性保證</b>，保證在工程版那邊：這裡只保證畫出來的每一格與每一條線都真的存在，
沒畫的都在右表點名。
</p>
</main></body></html>"""

(HERE / "index-owner-view.html").write_text(html, encoding="utf-8")
(HERE / "owner-model.json").write_text(json.dumps(
    {"nodes": [{"id": n[0], "col": n[1], "row": n[2], "side": n[3],
                "name": n[4], "role": n[5],
                "x": COL[n[1]] - NW / 2, "y": ROW[n[2]] - NH / 2, "w": NW, "h": NH}
               for n in NODES],
     "edges": [{"a": e[0], "b": e[2], "kind": e[4], "label": e[5]} for e in EDGES],
     "trace": TRACE, "cut": CUT, "numbers": NUM,
     "seam": SEAM, "viewbox": [W, H]},
    ensure_ascii=False, indent=2), encoding="utf-8")

print(f"nodes {len(NODES)}  edges {len(EDGES)}  (budget <=18 nodes / 24 hard)")
print(f"traced members {sum(len(v) for v in TRACE.values())}  cut {len(CUT)}")
print(f"numbers on page: {NUM}")
