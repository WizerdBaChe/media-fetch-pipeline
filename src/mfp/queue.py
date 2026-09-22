"""Task queue: state machine, removal policy, and persistence (PSM Batch 2 §4).

The queue is the GUI's centre of gravity, so its invariants matter more than
its convenience. Enforced here, one test each (§13):

  INV-1  identity is (platform, postId); a duplicate merges, never doubles
  INV-4  state survives restart; live states demote to PAUSED on load
  INV-5  COMPLETED never re-enters DOWNLOADING without an explicit retry
  INV-6  removing a record never deletes completed output files unless
         asked; the .part file is always removed
  INV-7  (in `inputs.py`) every automatic input change is reported
  INV-8  removing a non-terminal task cancels it first

The clock is injected, as in `budget.py`, so age-based sweeping is testable
without waiting three days.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import Field, ValidationError

from mfp.errors import MfpError
from mfp.inputs import ParsedItem
from mfp.models import CamelModel, Variant

ClockFn = Callable[[], float]
IdFn = Callable[[], str]

TaskState = Literal[
    "PARSED",
    "PROBING",
    "READY",
    "DOWNLOADING",
    "PAUSED",
    "EXPIRED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]

TERMINAL_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

#: Legal transitions (PSM Batch 2 §4.2). Anything not listed is rejected --
#: the state machine is a whitelist, so a new state cannot silently acquire
#: edges by omission.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "PARSED": frozenset({"PROBING", "CANCELLED", "FAILED"}),
    # PAUSED is reachable from here only by RESTART_RECOVERY -- no action and
    # no worker path produces it. It is listed because the recovery write is
    # a transition of this system whatever triggered it, and a table that
    # omits the edges only a crash can take is not a description of the
    # states a task can be in. Added 2026-08-23; before that `load()` wrote
    # it by direct assignment and the table said it could not happen.
    "PROBING": frozenset({"READY", "FAILED", "CANCELLED", "PAUSED"}),
    "READY": frozenset({"DOWNLOADING", "EXPIRED", "PROBING", "CANCELLED", "FAILED"}),
    "DOWNLOADING": frozenset({"COMPLETED", "PAUSED", "FAILED", "CANCELLED"}),
    "PAUSED": frozenset({"DOWNLOADING", "EXPIRED", "CANCELLED", "FAILED"}),
    # O-5 ruling: an expired task re-probes rather than asking the user.
    "EXPIRED": frozenset({"PROBING", "CANCELLED"}),
    # INV-5: the only way out of COMPLETED is an explicit retry, which
    # re-probes rather than jumping straight back into DOWNLOADING.
    "COMPLETED": frozenset({"PROBING"}),
    "FAILED": frozenset({"PROBING", "CANCELLED"}),
    "CANCELLED": frozenset({"PROBING"}),
}


#: Intent -> the transition it means, per current state (PSM §4.1).
#:
#: These are the semantic endpoints (`:start`, `:pause`, …). They exist so a
#: caller can express *what it wants* without knowing the state machine.
#: `:transition` remains as the primitive, but every ordinary consumer -- the
#: GUI, the Agent-facing Skill -- should use these: a caller that has to
#: compute "retry means go to PROBING" has to mirror LEGAL_TRANSITIONS, and
#: then there are two copies of the state machine to keep in step.
#:
#: A state absent from an intent's map means the intent does not apply there;
#: the endpoint answers 409 rather than inventing a transition.
ACTIONS: dict[str, dict[str, str]] = {
    # Read the page. Legal from anything that is not already probing.
    "probe": {
        "PARSED": "PROBING",
        "READY": "PROBING",
        "EXPIRED": "PROBING",
        "FAILED": "PROBING",
        "CANCELLED": "PROBING",
        "COMPLETED": "PROBING",
    },
    # "Make this go." One step toward downloaded, whatever that means here:
    # an unprobed task needs probing first, a ready one can transfer.
    "start": {
        "PARSED": "PROBING",
        "EXPIRED": "PROBING",
        "READY": "DOWNLOADING",
        "PAUSED": "DOWNLOADING",
    },
    "pause": {"DOWNLOADING": "PAUSED"},
    "cancel": {
        "PARSED": "CANCELLED",
        "PROBING": "CANCELLED",
        "READY": "CANCELLED",
        "DOWNLOADING": "CANCELLED",
        "PAUSED": "CANCELLED",
        "EXPIRED": "CANCELLED",
        "FAILED": "CANCELLED",
    },
    # INV-5: the only way out of COMPLETED, and it re-probes rather than
    # re-using a signature that is probably stale.
    "retry": {
        "FAILED": "PROBING",
        "CANCELLED": "PROBING",
        "COMPLETED": "PROBING",
        "EXPIRED": "PROBING",
    },
}


#: Where an in-flight state lands when the process died under it (G6 §5.1).
#:
#: A separate table because it is a separate question. The others describe
#: what a running system may do; this one describes what is true after it
#: stopped being one, and the answer is not "whatever the user last clicked".
#:
#: Each row must be an edge in `LEGAL_TRANSITIONS`, which `test_queue.py`
#: asserts. It was not, before 2026-08-23: `load()` demoted both in-flight
#: states with a direct assignment, and `PROBING -> PAUSED` was not an edge
#: the machine had. Nothing broke -- the worker re-probes on every start --
#: but the table stopped being a true description of the states a task can be
#: in, which is the only reason to keep one.
#:
#: The destinations are unchanged from that direct assignment, deliberately:
#: this round made the write declarative and checkable, not different. Whether
#: PROBING should instead recover to PARSED -- a task killed mid-probe has no
#: manifest, and PAUSED reads as "the bytes are part-way through" -- is a
#: question about what the row should SAY, and belongs to whoever owns the
#: wording rather than to a correctness fix.
RESTART_RECOVERY: dict[str, str] = {
    "PROBING": "PAUSED",
    "DOWNLOADING": "PAUSED",
}


class UnknownAction(MfpError):
    """An action name that is not in `ACTIONS`."""

    error_code = "usage_error"
    exit_code = 2


class IllegalTransition(MfpError):
    """Raised when a caller attempts a transition not in LEGAL_TRANSITIONS."""

    error_code = "illegal_transition"
    exit_code = 2


class TaskNotFound(MfpError):
    error_code = "task_not_found"
    exit_code = 2


# --- models -----------------------------------------------------------------


class TaskProgress(CamelModel):
    bytes_done: int = 0
    bytes_total: int | None = None
    bytes_per_sec: float = 0.0
    eta_seconds: float | None = None
    items_done: int = 0
    items_total: int | None = None
    #: Which half of a muxed item is running (D-35). `bytes_*` describe the
    #: transfers only and never move during `muxing`, so a byte counter
    #: alone reaches 100% and then sits there while ffmpeg works -- the bar
    #: says finished and the file does not exist yet. Null while nothing is
    #: running. M4 left this field out deliberately because it had no
    #: consumer until the worker existed to set it.
    phase: Literal["transferring", "muxing"] | None = None


class Task(CamelModel):
    """One queued post (PSM Batch 2 §4.3)."""

    id: str
    state: TaskState = "PARSED"
    platform: str
    post_id: str
    canonical_url: str
    source_url: str
    hint_index: int | None = None
    author: str | None = None
    title: str | None = None
    policy: str | None = None
    policy_pinned: bool = False
    #: Per-row 「一併存字幕」 override. `None` means inherit the global
    #: setting, which is the same three-state shape `policy` uses (null =
    #: inherit) -- a plain `False` here would be indistinguishable from
    #: 「the user turned it off for this row」 and a later change to the
    #: global default would silently overrule a deliberate per-row edit.
    write_subs: bool | None = None
    write_subs_pinned: bool = False
    variants: list[Variant] = Field(default_factory=list)
    chosen: Variant | None = None
    selected: bool = True
    selected_indices: list[int] | None = None
    expires_at: str | None = None
    progress: TaskProgress = Field(default_factory=TaskProgress)
    output_dir: str | None = None
    part_path: str | None = None
    path_degradation: Literal["L0", "L1"] = "L0"
    #: Q2: mirrors `Manifest.captions_unconfirmed` -- true only when the
    #: probe that filled this task's fields could not tell "no captions"
    #: from "could not ask" (P-84). Set from the manifest's own flag, never
    #: inferred here. Default False keeps every stored queue file valid.
    captions_unconfirmed: bool = False
    error_code: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return (self.platform, self.post_id)


class QueueFile(CamelModel):
    schema_version: Literal[1] = 1
    tasks: list[Task] = Field(default_factory=list)


class RemovalResult(CamelModel):
    """What a removal actually did -- the GUI reports these numbers back."""

    removed: int = 0
    cancelled_in_flight: int = 0
    part_files_deleted: int = 0
    output_files_deleted: int = 0
    #: Which records went, not just how many. Counts are enough for the tab
    #: that issued the removal -- it already knows what it asked for -- but
    #: not for any OTHER tab, which learns of the change only through the
    #: event stream and needs to know which rows to drop (R5-15).
    removed_ids: list[str] = Field(default_factory=list)


# --- the queue ---------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class TaskQueue:
    def __init__(
        self,
        state_path: Path,
        *,
        clock: ClockFn = time.time,
        new_id: IdFn = lambda: str(uuid.uuid4()),
    ) -> None:
        self._path = Path(state_path)
        self._clock = clock
        self._new_id = new_id
        self._tasks: dict[str, Task] = {}
        #: True when the state file was unreadable and the queue was rebuilt
        #: empty. Surfaced by `GET /v1/health` so the GUI can say so -- losing
        #: the queue without telling anyone is the failure mode to avoid.
        self.load_degraded = False
        self._load()

    # --- persistence --------------------------------------------------------

    def _load(self) -> None:
        """Load persisted tasks, failing safe to an empty queue.

        INV-4: a task recorded as PROBING or DOWNLOADING cannot actually be
        live after a restart -- the process that owned it is gone. Demote to
        PAUSED rather than resuming, so nothing starts moving bytes without
        the user asking.
        """
        if not self._path.exists():
            return
        try:
            # utf-8-sig: a BOM must not be mistaken for corruption. The G4
            # acceptance round has the user opening this file in Notepad, and
            # a save from there would otherwise take the whole queue down the
            # rebuild path below and rename it to `.corrupt`.
            data = json.loads(self._path.read_text(encoding="utf-8-sig"))
            loaded = QueueFile.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError):
            # Fail-safe rebuild, same policy as config.json / budget-state.json.
            # Two things beyond "don't crash", both required by PSM §4.5:
            #
            # 1. Record that it happened. The next save() overwrites the file,
            #    so without a flag the user's whole queue disappears with no
            #    account of why -- a silent failure, which this project treats
            #    as a defect.
            # 2. Keep the corrupt file. It is the only copy of whatever was in
            #    there, and destroying the evidence removes any chance of
            #    hand-recovering it.
            self._tasks = {}
            self.load_degraded = True
            try:
                self._path.replace(self._path.with_name(self._path.name + ".corrupt"))
            except OSError:
                # Best effort. Failing to preserve it must not stop startup.
                pass
            return

        now = _iso(self._clock())
        for task in loaded.tasks:
            recovered = RESTART_RECOVERY.get(task.state)
            if recovered is not None:
                task.state = recovered
                task.updated_at = now
            self._tasks[task.id] = task

    def save(self) -> None:
        """Atomic write (temp + os.replace), same volume."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = QueueFile(tasks=list(self._tasks.values()))
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # --- reads --------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise TaskNotFound(f"no task with id {task_id!r}") from None

    def list(self, state: str | None = None) -> list[Task]:
        tasks = list(self._tasks.values())
        if state is not None:
            tasks = [t for t in tasks if t.state == state]
        return sorted(tasks, key=lambda t: t.created_at)

    def find_by_identity(self, platform: str, post_id: str) -> Task | None:
        for task in self._tasks.values():
            if task.identity == (platform, post_id):
                return task
        return None

    def known_ids(self) -> set[tuple[str, str]]:
        """Feed this to `inputs.parse()` so dedup happens in one place."""
        return {t.identity for t in self._tasks.values()}

    # --- writes -------------------------------------------------------------

    def add(self, item: ParsedItem) -> Task:
        """Add a parsed item, merging on identity (INV-1).

        Merging updates `hintIndex` and `sourceUrl` from the newer paste --
        the user pasting again is a statement of current intent -- but never
        resets state or progress.
        """
        existing = self.find_by_identity(item.platform, item.post_id)
        now = _iso(self._clock())
        if existing is not None:
            if item.hint_index is not None:
                existing.hint_index = item.hint_index
            existing.source_url = item.source_url
            existing.updated_at = now
            return existing

        task = Task(
            id=self._new_id(),
            platform=item.platform,
            post_id=item.post_id,
            canonical_url=item.canonical_url,
            source_url=item.source_url,
            hint_index=item.hint_index,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.id] = task
        return task

    def add_many(
        self, items: list[ParsedItem], *, write_subs: bool | None = None
    ) -> list[Task]:
        """Add parsed items, optionally pinning 一併存字幕 on all of them.

        `write_subs` is what the user ticked in the add box for THIS paste.
        `None` leaves every new row inheriting the global setting, which is
        what a caller that never mentions captions gets.
        """
        tasks = [self.add(item) for item in items]
        if write_subs is not None:
            for task in tasks:
                task.write_subs = write_subs
                task.write_subs_pinned = True
        return tasks

    def transition(self, task_id: str, to_state: TaskState, *, error_code: str | None = None) -> Task:
        """Move a task, rejecting any edge not in LEGAL_TRANSITIONS."""
        task = self.get(task_id)
        allowed = LEGAL_TRANSITIONS.get(task.state, frozenset())
        if to_state not in allowed:
            raise IllegalTransition(
                f"{task.state} -> {to_state} is not a legal transition "
                f"(allowed: {sorted(allowed) or 'none'})"
            )
        now = _iso(self._clock())
        task.state = to_state
        task.updated_at = now
        task.error_code = error_code if to_state == "FAILED" else None
        if to_state == "COMPLETED":
            task.completed_at = now
        return task

    def action_target(self, task_id: str, action: str) -> str | None:
        """What `action` would do to this task, or None if it does not apply."""
        if action not in ACTIONS:
            raise UnknownAction(
                f"unknown action {action!r} (known: {sorted(ACTIONS)})"
            )
        return ACTIONS[action].get(self.get(task_id).state)

    def apply_action(self, task_id: str, action: str) -> Task:
        """Run a semantic action (PSM §4.1).

        Raises `IllegalTransition` when the action does not apply to the
        task's current state, rather than silently doing nothing -- a button
        that reports success without acting is worse than an error.
        """
        target = self.action_target(task_id, action)
        if target is None:
            state = self.get(task_id).state
            raise IllegalTransition(
                f"cannot {action} a task in {state} "
                f"(applies to: {sorted(ACTIONS[action])})"
            )
        return self.transition(task_id, target)

    def apply_action_many(self, task_ids: list[str], action: str) -> tuple[list[Task], int]:
        """Apply `action` where it applies; skip where it does not.

        Returns (changed, skipped). Bulk actions skip rather than raise: on
        "start everything", a queue holding one COMPLETED task is normal, not
        an error, and failing the whole call would be useless.
        """
        changed: list[Task] = []
        skipped = 0
        for task_id in task_ids:
            if self.action_target(task_id, action) is None:
                skipped += 1
                continue
            changed.append(self.apply_action(task_id, action))
        return changed, skipped

    def set_selected_many(self, task_ids: list[str] | None, selected: bool) -> list[Task]:
        """Set the checkbox on many rows at once. `None` means every row.

        Exists because the alternative -- one PATCH per row -- is what made
        "select all" cost N requests. A browser allows only six connections
        per origin over HTTP/1.1 and the event stream holds one of them for
        as long as the tab is open, so an N-row gesture does not merely cost
        N round trips: it starves everything else on the origin, including
        the other tab's stream. Measured 2026-08-16, R5-14/R5-15.

        Unknown ids are skipped rather than raising: the caller's list can
        legitimately race a removal, and failing the whole gesture over one
        stale id would lose the other thirteen.
        """
        targets = list(self._tasks) if task_ids is None else task_ids
        changed: list[Task] = []
        now = _iso(self._clock())
        for task_id in targets:
            task = self._tasks.get(task_id)
            if task is None or task.selected == selected:
                continue
            task.selected = selected
            task.updated_at = now
            changed.append(task)
        return changed

    def set_policy(self, task_id: str, policy: str | None) -> Task:
        """Set a per-row policy override and pin it (PSM §7.4).

        Pinning is the point: without it, a later change to the global
        policy would silently discard a deliberate per-row edit.
        """
        task = self.get(task_id)
        task.policy = policy
        task.policy_pinned = policy is not None
        task.updated_at = _iso(self._clock())
        return task

    def set_write_subs(self, task_id: str, write_subs: bool | None) -> Task:
        """Set a per-row 「一併存字幕」 override and pin it.

        Pinned for the reason `set_policy` is pinned: without it, changing the
        global setting afterwards would discard a deliberate per-row edit. The
        difference from `policy` is only that the value is a tri-state rather
        than a string, so `None` has to be passed explicitly to un-pin.
        """
        task = self.get(task_id)
        task.write_subs = write_subs
        task.write_subs_pinned = write_subs is not None
        task.updated_at = _iso(self._clock())
        return task

    def mark_expired_if_due(self, task_id: str) -> Task:
        """Move READY/PAUSED to EXPIRED when `expiresAt` has passed.

        Checked before a transfer starts rather than on a timer: the only
        moment expiry matters is the moment we are about to use the URL.
        """
        task = self.get(task_id)
        if task.state not in ("READY", "PAUSED") or task.expires_at is None:
            return task
        try:
            expiry = datetime.fromisoformat(task.expires_at)
        except ValueError:
            return task
        if expiry.timestamp() <= self._clock():
            self.transition(task_id, "EXPIRED")
        return task

    # --- removal (O-6) ------------------------------------------------------

    def _remove_one(self, task: Task, *, delete_files: bool, result: RemovalResult) -> None:
        if task.state not in TERMINAL_STATES:
            # INV-8: a transfer must never outlive its own record.
            task.state = "CANCELLED"
            result.cancelled_in_flight += 1

        # INV-6: the .part file is ours, and an orphaned one has no owner and
        # no resumer -- always remove it. Completed outputs are the user's.
        if task.part_path:
            part = Path(task.part_path)
            try:
                if part.is_file():
                    part.unlink()
                    result.part_files_deleted += 1
            except OSError:
                pass  # a locked temp file must not block record removal

        if delete_files and task.output_dir:
            result.output_files_deleted += self._delete_output_dir(Path(task.output_dir))

        self._tasks.pop(task.id, None)
        result.removed += 1
        result.removed_ids.append(task.id)

    @staticmethod
    def _delete_output_dir(directory: Path) -> int:
        deleted = 0
        try:
            if not directory.is_dir():
                return 0
            for child in sorted(directory.iterdir(), reverse=True):
                if child.is_file():
                    child.unlink()
                    deleted += 1
            directory.rmdir()
        except OSError:
            pass
        return deleted

    def remove(self, task_ids: list[str], *, delete_files: bool = False) -> RemovalResult:
        """Remove the given records (「刪除勾選」). Unknown ids are ignored."""
        result = RemovalResult()
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task is not None:
                self._remove_one(task, delete_files=delete_files, result=result)
        return result

    def clear_completed(self) -> RemovalResult:
        """Remove every COMPLETED record. Never touches files."""
        ids = [t.id for t in self._tasks.values() if t.state == "COMPLETED"]
        return self.remove(ids, delete_files=False)

    def clear_all(self) -> RemovalResult:
        """Remove every record (「一鍵刪除紀錄」), cancelling in-flight work.

        Never deletes completed output files -- the GUI must still confirm
        first, because cancelling in-flight transfers is not free even
        though nothing on disk is lost.
        """
        return self.remove(list(self._tasks.keys()), delete_files=False)

    def sweep(self, *, max_age_days: float) -> RemovalResult:
        """O-6 age-based sweep of COMPLETED records. `0` disables it."""
        if max_age_days <= 0:
            return RemovalResult()
        cutoff = self._clock() - max_age_days * 86400
        stale: list[str] = []
        for task in self._tasks.values():
            if task.state != "COMPLETED" or task.completed_at is None:
                continue
            try:
                completed = datetime.fromisoformat(task.completed_at).timestamp()
            except ValueError:
                continue
            if completed <= cutoff:
                stale.append(task.id)
        return self.remove(stale, delete_files=False)
