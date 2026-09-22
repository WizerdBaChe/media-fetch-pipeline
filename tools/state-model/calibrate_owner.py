"""Six needles for verify_owner.py, one per class of lie the owner view
could tell. Each is planted in build_owner.py -- UPSTREAM of everything the
checker reads -- then the page is rebuilt and the checker must fail.

  O1  a box that stands for something the canonical model does not have
  O2  a module that quietly disappears (drawn nowhere, cut-list nowhere)
  O3  a box drawn on the wrong side of the seam
  O4  a number typed into the prose instead of computed
  O5  a connector routed through an unrelated box
  O6  a status row that loses its anchor back to the canonical wording

build_owner.py is restored byte-for-byte and the restore is verified by
hash, not assumed.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BUILD = HERE / "build_owner.py"
ORIG = BUILD.read_bytes()
ORIG_SHA = hashlib.sha256(ORIG).hexdigest()
PY = sys.executable

NEEDLES = [
    ("O1 a box stands for a module that does not exist  -> rung 0 invented",
     '"stack":  ["stack"],', '"stack":  ["stack", "summarise"],'),
    ("O2 a module vanishes from both the picture and the cut list  -> rung 0 orphan",
     '    "doctor": "診斷指令，不在主流程上",\n', ''),
    ("O3 a box on the wrong side of the seam  -> rung 0 side + rung 2 seam",
     '("stack",  "c1", "r3", "stay",', '("stack",  "c1", "r3", "go",  '),
    ("O4 a number typed into the prose  -> rung 1 hand-typed number",
     '"今天卡住的不是技術，', '"這佔了大約 41% 的工作量，今天卡住的不是技術，'),
    ("O5 a connector routed through an unrelated box  -> rung 2 pierced",
     '("brief",  "c1", "r4", "stay",', '("brief",  "c2", "r4", "stay",'),
    ("O6 a status row loses its anchor to the canonical wording  -> rung 1 anchors",
     ' data-canon="{esc(r["item"])}"', ''),
]

#: The needles above are written with `\n`, and the file they are planted in
#: is matched as BYTES so the restore can be verified by hash. Those two facts
#: collided on 2026-09-09: this bundle moved into the repository, git's
#: `.gitattributes` normalised it to CRLF, and O2 -- the only needle that
#: spans a line break -- stopped matching. It reported VOID rather than
#: passing, which is the one thing that made it survivable.
#:
#: A control may not depend on a byte that version control rewrites. The
#: needle is translated to whatever the file actually uses, so the same needle
#: works on either convention and a future re-normalisation cannot silently
#: retire it.
NEWLINE = "\r\n" if b"\r\n" in ORIG else "\n"


def _as_written(text: str) -> str:
    return text.replace("\n", NEWLINE) if NEWLINE != "\n" else text


def run(script: str) -> int:
    return subprocess.run([PY, str(HERE / script)], capture_output=True).returncode


def rebuild_and_verify() -> int:
    if run("build_owner.py") != 0:
        return -1                       # the build broke: the control is void
    return run("verify_owner.py")


print("negative controls (each must FIRE):")
results = []
for label, old, new in NEEDLES:
    src = ORIG.decode("utf-8")
    old, new = _as_written(old), _as_written(new)
    if src.count(old) != 1:
        print(f"  VOID    {label}   (needle matched {src.count(old)}x, not once)")
        results.append(False)
        continue
    BUILD.write_bytes(src.replace(old, new).encode("utf-8"))
    rc = rebuild_and_verify()
    BUILD.write_bytes(ORIG)
    fired = rc > 0
    print(f"  {'FIRED ' if fired else 'SILENT'}  {label}   (verify exit {rc})")
    if rc == -1:
        print("           build_owner.py crashed -- this control proved NOTHING")
    results.append(fired)

print("\npositive control (must PASS):")
rc = rebuild_and_verify()
print(f"  {'PASS  ' if rc == 0 else 'FAIL  '}  untouched build   (verify exit {rc})")

after = hashlib.sha256(BUILD.read_bytes()).hexdigest()
print(f"\nbuild_owner.py restored byte-for-byte: {after == ORIG_SHA}")

ok = all(results) and rc == 0 and after == ORIG_SHA
print("\nCALIBRATED" if ok else "\nNOT CALIBRATED -- a control did not behave")
sys.exit(0 if ok else 1)
