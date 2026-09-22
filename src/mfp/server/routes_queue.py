"""Queue and input endpoints (PSM Batch 2 GUI §4.1).

Every mutating endpoint persists before returning, so a crash immediately
after a response cannot lose the state the client was just told about.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from mfp.inputs import ParseResult, parse
from mfp.models import CamelModel
from mfp.queue import RemovalResult, Task, TaskQueue, TaskState
from mfp.server.platform_gate import current_blocked_platforms


class ParseRequest(CamelModel):
    text: str


class AddRequest(CamelModel):
    """Add already-parsed items. The GUI calls `/input/parse` first so the
    user can see the change report before anything enters the queue."""

    text: str
    #: 一併存字幕 for this paste only. `None` -- what every existing caller
    #: sends, since the field is new -- leaves the rows inheriting the global
    #: setting, so an agent that never heard of captions keeps its behaviour.
    write_subs: bool | None = None


class AddResponse(CamelModel):
    tasks: list[Task] = Field(default_factory=list)
    report: dict = Field(default_factory=dict)


class PatchRequest(CamelModel):
    policy: str | None = None
    clear_policy: bool = False
    #: Tri-state, so it needs its own clear flag for the same reason
    #: `clear_policy` exists: `None` on the wire is 「not mentioned in this
    #: PATCH」 and cannot also mean 「go back to inheriting」.
    write_subs: bool | None = None
    clear_write_subs: bool = False
    selected: bool | None = None
    selected_indices: list[int] | None = None


class TransitionRequest(CamelModel):
    to: TaskState
    error_code: str | None = None


class BulkActionRequest(CamelModel):
    """Empty body (or empty `ids`) means "the selected rows"."""

    ids: list[str] = Field(default_factory=list)


class BulkActionResult(CamelModel):
    action: str
    changed: int = 0
    #: Rows the action did not apply to. Not an error: "start everything" on a
    #: queue holding a COMPLETED task is normal.
    skipped: int = 0
    tasks: list[Task] = Field(default_factory=list)


class SelectRequest(CamelModel):
    """Omit `ids` to mean every row in the queue."""

    ids: list[str] | None = None
    selected: bool


class RemoveRequest(CamelModel):
    ids: list[str] = Field(default_factory=list)
    delete_files: bool = False


class SweepRequest(CamelModel):
    max_age_days: float = 3.0


def _queue(request: Request) -> TaskQueue:
    return request.app.state.queue


def _worker(request: Request):
    """The transfer worker, or None in a build without one.

    None is a real configuration, not a defect: `create_app()` is used by
    tests and by anything that wants the API without a downloader. Every
    call site treats it as "the buttons still change state, nothing moves
    bytes", which is exactly what `/v1/health` reports.
    """
    return getattr(request.app.state, "worker", None)


#: Which action means "go and do the work", and how far. `pause` and
#: `cancel` are the other direction -- they stop an in-flight job. Written
#: out rather than derived from ACTIONS because "which actions start work"
#: is a different question from "which transitions are legal", and deriving
#: one from the other is how a new action silently becomes a no-op button.
_WORK_INTENT: dict[str, str] = {"probe": "probe", "start": "start", "retry": "start"}
_STOP_ACTIONS = frozenset({"pause", "cancel"})


def _dispatch_work(request: Request, action: str, tasks: list[Task]) -> None:
    worker = _worker(request)
    if worker is None:
        return
    intent = _WORK_INTENT.get(action)
    for task in tasks:
        if intent is not None:
            worker.submit(task, intent)  # type: ignore[arg-type]
        elif action in _STOP_ACTIONS:
            worker.request_stop(task.id)


#: Which states each queueStats counter covers. Written out rather than
#: derived so a new state cannot be silently absent from every counter.
_ACTIVE_STATES = frozenset({"PROBING", "DOWNLOADING"})
_QUEUED_STATES = frozenset({"PARSED", "READY", "PAUSED"})
_DONE_STATES = frozenset({"COMPLETED"})
_FAILED_STATES = frozenset({"FAILED", "EXPIRED", "CANCELLED"})


def queue_stats(queue: TaskQueue, governor: object | None) -> dict[str, object]:
    """The `queueStats` payload (audit B-1).

    Specified in the PSM and consumed by the status bar since G4, but never
    published -- so the budget counter could not appear no matter what the
    governor did. `budgetRemainingThisHour` stays null until an adapter owns
    a governor; null renders as "—", which is honest, whereas zero would
    read as "you are out of quota".
    """
    tasks = queue.list()
    remaining: int | None = None
    if governor is not None:
        try:
            remaining = governor.snapshot("instagram").requests_remaining_hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 -- a stats read must never fail a mutation
            remaining = None

    return {
        "active": sum(1 for task in tasks if task.state in _ACTIVE_STATES),
        "queued": sum(1 for task in tasks if task.state in _QUEUED_STATES),
        "done": sum(1 for task in tasks if task.state in _DONE_STATES),
        "failed": sum(1 for task in tasks if task.state in _FAILED_STATES),
        "aggregateBytesPerSec": sum(
            task.progress.bytes_per_sec for task in tasks if task.state in _ACTIVE_STATES
        ),
        "budgetRemainingThisHour": remaining,
    }


def _broadcast_stats(request: Request) -> None:
    request.app.state.broadcaster.publish(
        "queueStats",
        queue_stats(_queue(request), getattr(request.app.state, "budget", None)),
    )


def _broadcast_task(request: Request, task: Task) -> None:
    request.app.state.broadcaster.publish(
        "task", task.model_dump(by_alias=True, mode="json")
    )


def build_queue_router() -> APIRouter:
    router = APIRouter(tags=["queue"])

    # --- input (pure, no network, no budget cost) --------------------------

    @router.post("/input/parse", response_model=ParseResult)
    async def parse_input(request: Request, body: ParseRequest) -> ParseResult:
        return parse(
            body.text,
            known_ids=_queue(request).known_ids(),
            blocked_platforms=current_blocked_platforms(request),
        )

    # --- queue reads -------------------------------------------------------

    @router.get("/queue", response_model=list[Task])
    async def list_tasks(request: Request, state: TaskState | None = Query(default=None)) -> list[Task]:
        return _queue(request).list(state=state)

    @router.get("/queue/{task_id}", response_model=Task)
    async def get_task(request: Request, task_id: str) -> Task:
        return _queue(request).get(task_id)

    # --- queue writes ------------------------------------------------------

    @router.post("/queue", response_model=AddResponse, status_code=201)
    async def add_tasks(request: Request, body: AddRequest) -> AddResponse:
        queue = _queue(request)
        # The same gate as /input/parse, and not merely for consistency: an
        # agent or a second client can POST here without ever calling the
        # preview endpoint, and a door that is only locked on one side is
        # the defect this project keeps finding in itself.
        result = parse(
            body.text,
            known_ids=queue.known_ids(),
            blocked_platforms=current_blocked_platforms(request),
        )
        tasks = queue.add_many(result.items, write_subs=body.write_subs)
        queue.save()
        for task in tasks:
            _broadcast_task(request, task)
        _broadcast_stats(request)
        return AddResponse(
            tasks=tasks, report=result.report.model_dump(by_alias=True, mode="json")
        )

    @router.patch("/queue/{task_id}", response_model=Task)
    async def patch_task(request: Request, task_id: str, body: PatchRequest) -> Task:
        queue = _queue(request)
        task = queue.get(task_id)
        if body.clear_policy:
            queue.set_policy(task_id, None)
        elif body.policy is not None:
            queue.set_policy(task_id, body.policy)
        if body.clear_write_subs:
            queue.set_write_subs(task_id, None)
        elif body.write_subs is not None:
            queue.set_write_subs(task_id, body.write_subs)
        if body.selected is not None:
            task.selected = body.selected
        if body.selected_indices is not None:
            task.selected_indices = body.selected_indices
        queue.save()
        _broadcast_task(request, task)
        _broadcast_stats(request)
        return task

    @router.post("/queue:select", response_model=BulkActionResult)
    async def select_tasks(request: Request, body: SelectRequest) -> BulkActionResult:
        """Tick or untick many rows in one request (see `set_selected_many`)."""
        queue = _queue(request)
        changed = queue.set_selected_many(body.ids, body.selected)
        queue.save()
        for task in changed:
            _broadcast_task(request, task)
        _broadcast_stats(request)
        return BulkActionResult(
            action="select" if body.selected else "deselect",
            changed=len(changed),
            tasks=changed,
        )

    @router.post("/queue/{task_id}:transition", response_model=Task)
    async def transition_task(request: Request, task_id: str, body: TransitionRequest) -> Task:
        queue = _queue(request)
        task = queue.transition(task_id, body.to, error_code=body.error_code)
        queue.save()
        _broadcast_task(request, task)
        _broadcast_stats(request)
        return task

    # --- semantic actions (PSM §4.1) ---------------------------------------
    #
    # `:transition` above is the primitive and stays for tooling that really
    # does want to name a state. Everything else should use these: a caller
    # that has to know "retry means PROBING" ends up mirroring the state
    # machine, and then there are two copies to keep in step.

    def _action_route(name: str):
        @router.post(f"/queue/{{task_id}}:{name}", response_model=Task, name=f"task_{name}")
        async def run(request: Request, task_id: str) -> Task:
            queue = _queue(request)
            task = queue.apply_action(task_id, name)
            queue.save()
            # Persist before dispatching: the worker's first act is to
            # transition the same row, and a crash between the two must
            # leave the state the client was just told about (§4.1).
            _dispatch_work(request, name, [task])
            _broadcast_task(request, task)
            _broadcast_stats(request)
            return task

        return run

    for _action in ("probe", "start", "pause", "cancel", "retry"):
        _action_route(_action)

    @router.post("/queue:startAll", response_model=BulkActionResult)
    async def start_all(request: Request, body: BulkActionRequest | None = None) -> BulkActionResult:
        return _run_bulk(request, "start", body)

    @router.post("/queue:pauseAll", response_model=BulkActionResult)
    async def pause_all(request: Request, body: BulkActionRequest | None = None) -> BulkActionResult:
        return _run_bulk(request, "pause", body)

    def _run_bulk(
        request: Request, action: str, body: BulkActionRequest | None
    ) -> BulkActionResult:
        queue = _queue(request)
        if body is not None and body.ids:
            targets = body.ids
        else:
            # Default to the *selected* rows, not every row: the checkbox is
            # the user's statement of what they meant (§6, §7.2).
            targets = [task.id for task in queue.list() if task.selected]

        changed, skipped = queue.apply_action_many(targets, action)
        queue.save()
        _dispatch_work(request, action, changed)
        for task in changed:
            _broadcast_task(request, task)
        _broadcast_stats(request)
        return BulkActionResult(
            action=action, changed=len(changed), skipped=skipped, tasks=changed
        )

    # --- removal (O-6) -----------------------------------------------------

    def _finish_removal(request: Request, result: RemovalResult) -> RemovalResult:
        queue = _queue(request)
        queue.save()
        # INV-8: a transfer must never outlive its own record. The queue
        # marked the row CANCELLED and deleted the `.part`; without this the
        # worker would keep writing to a file the queue has already removed.
        worker = _worker(request)
        if worker is not None:
            for task_id in result.removed_ids:
                worker.request_stop(task_id)
        broadcaster = request.app.state.broadcaster
        # A `notice` is prose; it tells another tab that something happened
        # but not what to do about it, so that tab went on showing rows the
        # server no longer had (measured with two tabs, 2026-08-16 -- server
        # 22 rows, both tabs 23). `removed` is the state change itself.
        if result.removed_ids:
            broadcaster.publish("removed", {"ids": result.removed_ids})
        # Fields, not a sentence. This module is English by contract, so any
        # prose written here reaches the user in the wrong language -- which
        # it did: a bulk removal answered with an English banner in the
        # middle of a Chinese UI. `presentNotice` writes the sentence.
        broadcaster.publish(
            "notice",
            {
                "level": "info",
                "code": "records_removed",
                "removed": result.removed,
                "cancelledInFlight": result.cancelled_in_flight,
            },
        )
        _broadcast_stats(request)
        return result

    @router.delete("/queue/{task_id}", response_model=RemovalResult)
    async def delete_task(
        request: Request, task_id: str, delete_files: bool = Query(default=False)
    ) -> RemovalResult:
        queue = _queue(request)
        queue.get(task_id)  # 404 rather than a silent no-op
        return _finish_removal(
            request, queue.remove([task_id], delete_files=delete_files)
        )

    @router.post("/queue:remove", response_model=RemovalResult)
    async def remove_tasks(request: Request, body: RemoveRequest) -> RemovalResult:
        return _finish_removal(
            request, _queue(request).remove(body.ids, delete_files=body.delete_files)
        )

    @router.post("/queue:clearCompleted", response_model=RemovalResult)
    async def clear_completed(request: Request) -> RemovalResult:
        return _finish_removal(request, _queue(request).clear_completed())

    @router.post("/queue:clearAll", response_model=RemovalResult)
    async def clear_all(request: Request) -> RemovalResult:
        return _finish_removal(request, _queue(request).clear_all())

    @router.post("/queue:sweep", response_model=RemovalResult)
    async def sweep(request: Request, body: SweepRequest | None = None) -> RemovalResult:
        max_age = body.max_age_days if body is not None else 3.0
        return _finish_removal(request, _queue(request).sweep(max_age_days=max_age))

    # --- events ------------------------------------------------------------

    @router.get("/events")
    async def events(request: Request) -> StreamingResponse:
        broadcaster = request.app.state.broadcaster
        queue = broadcaster.subscribe()
        return StreamingResponse(
            broadcaster.stream(queue),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
