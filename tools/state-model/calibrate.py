"""Negative control: plant a defect UPSTREAM of everything verify.py reads,
rebuild, and require the checker to fail. A gate that has never failed is a
gate whose failure path is untested.

Three needles, one per class of claim the checker makes:
  N1  a renamed module        -> rung 1 must report it missing
  N2  a module moved to the   -> rung 2's side-of-the-seam assert must fire
      wrong side of the seam
  N3  two chips forced to     -> rung 2's overlap assert must fire
      overlap

The model file is restored byte-for-byte afterwards, and the restore is
verified by hash rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable
MODEL = HERE / "model.json"
ORIG = MODEL.read_bytes()
ORIG_SHA = hashlib.sha256(ORIG).hexdigest()


def run(script: str) -> int:
    return subprocess.run([PY, str(HERE / script)], capture_output=True).returncode


def rebuild_and_verify() -> int:
    if run("build.py") != 0:
        return -1                      # build broke: the control is invalid
    return run("verify.py")


def with_model(mutate, label: str, patch_build: str | None = None) -> bool:
    data = json.loads(ORIG.decode("utf-8"))
    mutate(data)
    MODEL.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    backup = None
    if patch_build:
        bp = HERE / "build.py"
        backup = bp.read_text(encoding="utf-8")
        bp.write_text(backup.replace(*patch_build), encoding="utf-8")
    rc = rebuild_and_verify()
    if backup is not None:
        (HERE / "build.py").write_text(backup, encoding="utf-8")
    MODEL.write_bytes(ORIG)
    fired = rc > 0
    print(f"  {'FIRED ' if fired else 'SILENT'}  {label}   (verify exit {rc})")
    if rc == -1:
        print("           build.py crashed -- this control proved NOTHING")
    return fired


def n1(d):
    """A module put on the wrong side of the core/extension line."""
    core = next(z for z in d["zones"] if z["id"] == "core")
    go = next(z for z in d["zones"] if z["id"] == "ext_go")
    moved = [m for m in core["members"] if m["n"] == "toolchain"][0]
    core["members"].remove(moved)
    go["members"].append(moved)


def n2(d):
    """A line count that flatters the diagram."""
    go = next(z for z in d["zones"] if z["id"] == "ext_go")
    [m for m in go["members"] if m["n"] == "asr"][0]["lines"] = 120


def n3(d):
    """A module invented -- present in the picture, absent from the gate."""
    next(z for z in d["zones"] if z["id"] == "ext_go")["members"].append(
        {"n": "summarise", "lines": 400}
    )


def n5(d):
    """A zone TOTAL that no longer matches the members under it. This is the
    real defect that got through: gui_go printed 4,261 while its own members
    summed to 4,278, and nothing looked at subtitles."""
    next(z for z in d["zones"] if z["id"] == "gui_go")["subtitle"] = "gui/src/ 之下，4,261 行"


print("negative controls (each must FIRE):")
results = [
    with_model(n1, "N1 module on the wrong side of core/extension -> rung 0"),
    with_model(n2, "N2 wrong line count                            -> rung 0"),
    with_model(n3, "N3 invented module                             -> rung 0"),
    # Geometry cannot be reached through the model -- the grid makes overlap
    # impossible by construction -- so the needle goes into the RENDERER, which
    # is where a layout defect would really enter.
    with_model(lambda d: None,
               "N4 chip wider than its column   -> rung 2 overlap assert",
               patch_build=("CHIP_W = (LEFT_W - 2 * PAD - (COLS - 1) * CHIP_GAP) / COLS",
                            "CHIP_W = (LEFT_W - 2 * PAD - (COLS - 1) * CHIP_GAP) / COLS + 30")),
    with_model(n5, "N5 zone subtitle total drifts from its members  -> rung 0"),
]

print("\npositive control (must PASS):")
rc = rebuild_and_verify()
print(f"  {'PASS  ' if rc == 0 else 'FAIL  '}  untouched model   (verify exit {rc})")

after = hashlib.sha256(MODEL.read_bytes()).hexdigest()
print(f"\nmodel.json restored byte-for-byte: {after == ORIG_SHA}")
print(f"  before {ORIG_SHA[:16]}…\n  after  {after[:16]}…")

ok = all(results) and rc == 0 and after == ORIG_SHA
print("\nCALIBRATED" if ok else "\nNOT CALIBRATED -- a control did not behave")
sys.exit(0 if ok else 1)
