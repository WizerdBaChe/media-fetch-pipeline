"""Where the Skill lives, and how it gets in front of an agent (O-9).

M9 shipped `skill/SKILL.md` and the `mfp` CLI and stopped there, which was
enough for a checkout and nothing at all for an installed machine: the
installer laid down the Electron app and a sidecar whose only subcommand is
`serve`, so the Skill taught agents to call commands that were not present.
Closing that gap is three separate problems solved in three different
places -- this module is the middle one.

  ship it        the `mfp` entry point is built and installed beside the
                 sidecar (`scripts/build-all.ps1`), and `skill/SKILL.md`
                 travels INSIDE the binary rather than beside it, so there
                 is no second copy to drift.
  find it        the installer can put the install directory on PATH. That
                 is the only discovery mechanism that is not vendor-specific.
  understand it  `mfp agent-guide` prints the whole contract on stdout. One
                 command, no file hunting, no vendor.

**There is no cross-vendor standard for a locally installed CLI to announce
itself to an agent.** What exists is per-vendor or per-repo:

  ~/.claude/skills/<name>/SKILL.md   Claude Code, personal scope
  ~/.codex/AGENTS.md                 Codex, global scope
  ~/.factory/AGENTS.md               droid, global scope
  <repo>/AGENTS.md                   read by many tools -- but repo-scoped,
                                     so it helps only inside that repo
  ~/.config/agents/AGENTS.md         proposed cross-tool location, not
                                     adopted; deliberately not written here

So `agent-register` writes to a target the CALLER names. Nothing in this
module runs on its own and nothing here guesses: registering this tool with
somebody's agent means writing into that agent's configuration, which is the
user's call (the installer's tick-box) or their agent's call (`mfp
agent-register`), never this program's.

The fallback when none of that has happened is deliberately dull and always
works: the tool is on PATH, `mfp --help` names `agent-guide`, and
`agent-guide` prints the contract.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from mfp.errors import MfpError, UsageError

#: Both markers are written into files that belong to somebody else, so they
#: have to be findable by a later run and by a human reading a diff. What is
#: between them is ours to replace; what is outside them is never touched.
BEGIN_MARKER = "<!-- media-fetch-pipeline:begin -->"
END_MARKER = "<!-- media-fetch-pipeline:end -->"

#: The directory name used under a skills root.
SKILL_NAME = "media-fetch-pipeline"

#: Kept short on purpose. The published guidance on agent context files is
#: that they help only while they stay minimal and precise, and the full
#: contract is one command away rather than duplicated here -- a copy of the
#: exit-code table in three config files is three copies that go stale.
POINTER_BLOCK = f"""{BEGIN_MARKER}
## media-fetch-pipeline (`mfp`)

Local CLI that saves the media from a **single public post URL** (Instagram,
Threads, YouTube, X, Bilibili, and most yt-dlp hosts). Use it when the user
pastes a post link and wants the file; do not write a downloader.

```bash
mfp probe <url> --json   # what is available, without downloading
mfp fetch <url> --json   # download it -- parse stdout only
mfp agent-guide          # the full contract: exit codes, what never to retry
```

Never retry on exit 4 (blocked upstream) or exit 7 (local pacing budget
spent). It never logs in and never crawls a feed or a profile.
{END_MARKER}"""


def skill_path() -> Path:
    """Locate SKILL.md in both a checkout and a frozen build.

    PyInstaller lays `--add-data` payloads under `sys._MEIPASS` (the bundle
    root for onefile, `_internal` for onedir). The checkout branch walks up
    from this file rather than using the working directory, because an agent
    calls `mfp` from wherever the user happens to be.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "agent" / "SKILL.md"
    return Path(__file__).resolve().parents[2] / "skill" / "SKILL.md"


def read_skill() -> str:
    """The Skill's text, or a failure naming the path it looked at.

    A frozen build with the data payload left out would otherwise print an
    empty guide and look like it worked, which is the failure mode this
    whole module exists to remove.
    """
    path = skill_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MfpError(f"the packaged Skill is missing at {path}: {exc}") from exc


@dataclass(frozen=True)
class Target:
    """One place a Skill can be registered.

    `kind` decides the write strategy, and the two strategies are genuinely
    different: a skills directory is ours to own, so the file is replaced
    whole; an AGENTS.md belongs to the user, so only the marked block is.
    """

    key: str
    kind: str  # "directory" | "markdown"
    default: Path
    note: str


def targets() -> dict[str, Target]:
    """The named destinations, resolved against the current user's home.

    Built per call rather than at import so a test can move HOME.
    """
    home = Path.home()
    return {
        "claude": Target(
            key="claude",
            kind="directory",
            default=home / ".claude" / "skills" / SKILL_NAME,
            note="Claude Code personal skills",
        ),
        "codex": Target(
            key="codex",
            kind="markdown",
            default=home / ".codex" / "AGENTS.md",
            note="Codex global instructions",
        ),
        "droid": Target(
            key="droid",
            kind="markdown",
            default=home / ".factory" / "AGENTS.md",
            note="droid global instructions",
        ),
        "agents-md": Target(
            key="agents-md",
            kind="markdown",
            default=Path.cwd() / "AGENTS.md",
            note="an AGENTS.md in the current directory",
        ),
    }


def resolve_target(key: str, override: Path | None = None) -> tuple[Target, Path]:
    """Pick a target and the path to act on, refusing an unknown name."""
    known = targets()
    target = known.get(key)
    if target is None:
        raise UsageError(f"unknown target {key!r}; choose one of: {', '.join(sorted(known))}")
    return target, (override if override is not None else target.default)


def splice(existing: str, block: str) -> str:
    """Replace the marked block, or append it, leaving the rest untouched."""
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if start != -1 and end != -1 and end > start:
        return existing[:start] + block + existing[end + len(END_MARKER) :]
    if not existing.strip():
        return block + "\n"
    if existing.endswith("\n\n"):
        separator = ""
    elif existing.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return existing + separator + block + "\n"


def strip_block(existing: str) -> str:
    """Remove the marked block. Anything outside the markers survives."""
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        return existing
    head = existing[:start].rstrip("\n")
    tail = existing[end + len(END_MARKER) :].lstrip("\n")
    remainder = f"{head}\n{tail}" if head else tail
    return "" if not remainder.strip() else remainder


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise MfpError(f"cannot read {path}: {exc}") from exc


def _write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise MfpError(f"cannot write {path}: {exc}") from exc


@dataclass(frozen=True)
class Plan:
    """What a run would do, decided before anything is written.

    `--dry-run` and the real thing take the SAME path through this, so the
    preview cannot describe an action the writer would not perform.
    """

    target: str
    kind: str
    path: Path
    action: str  # create | update | unchanged | remove | absent
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "kind": self.kind,
            "path": str(self.path),
            "action": self.action,
            "detail": self.detail,
        }


def plan_install(key: str, override: Path | None = None) -> Plan:
    target, path = resolve_target(key, override)

    if target.kind == "directory":
        destination = path / "SKILL.md"
        wanted = read_skill()
        if not destination.exists():
            action, detail = "create", f"write the Skill to {destination}"
        elif _read(destination) == wanted:
            action, detail = "unchanged", f"{destination} already matches this build"
        else:
            action, detail = "update", f"replace {destination} with this build's Skill"
        return Plan(target.key, target.kind, destination, action, detail)

    existing = _read(path)
    updated = splice(existing, POINTER_BLOCK)
    if not path.exists():
        action, detail = "create", f"create {path} with the pointer block"
    elif updated == existing:
        action, detail = "unchanged", f"{path} already carries the current pointer block"
    elif BEGIN_MARKER in existing:
        action, detail = "update", f"replace the marked block in {path}"
    else:
        action, detail = "update", f"append the marked block to {path}"
    return Plan(target.key, target.kind, path, action, detail)


def plan_remove(key: str, override: Path | None = None) -> Plan:
    target, path = resolve_target(key, override)

    if target.kind == "directory":
        destination = path / "SKILL.md"
        if not destination.exists():
            return Plan(target.key, target.kind, destination, "absent", f"{destination} is not there")
        return Plan(target.key, target.kind, destination, "remove", f"delete {destination}")

    if BEGIN_MARKER not in _read(path):
        return Plan(target.key, target.kind, path, "absent", f"{path} carries no marked block")
    return Plan(target.key, target.kind, path, "remove", f"remove the marked block from {path}")


def apply_plan(plan: Plan, key: str, override: Path | None = None) -> Plan:
    """Carry out a plan. `unchanged` and `absent` write nothing, by design."""
    if plan.action in {"unchanged", "absent"}:
        return plan

    target, path = resolve_target(key, override)

    if target.kind == "directory":
        if plan.action == "remove":
            plan.path.unlink(missing_ok=True)
            # Only if we are the last thing in it: a skills root the user
            # keeps other skills in is not ours to tidy.
            try:
                plan.path.parent.rmdir()
            except OSError:
                pass
            return plan
        _write(plan.path, read_skill())
        return plan

    existing = _read(path)
    if plan.action == "remove":
        stripped = strip_block(existing)
        if stripped:
            _write(path, stripped)
        else:
            path.unlink(missing_ok=True)
        return plan

    _write(path, splice(existing, POINTER_BLOCK))
    return plan


def which_mfp() -> str | None:
    """Where an agent would find this CLI on PATH, or None if it would not.

    Reported by `agent-guide --json` because "the tool is installed" and "an
    agent can discover the tool" are different facts, and the second one is
    what O-9 was about.
    """
    return shutil.which("mfp")
