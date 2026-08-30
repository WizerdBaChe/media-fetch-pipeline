"""`stack` as a job the GUI can start, watch, and stop.

Deliberately NOT a second kind of queue task. A download and a stacked quote
image are different work: the queue's states (PROBING, DOWNLOADING, EXPIRED)
and its progress unit (bytes) describe a transfer, and a job whose real
phases are "scan a band" and "render twenty-four frames" would have to
borrow them and lie. The event bus is shared, the vocabulary is not.

Three things this module is careful about:

**One job at a time.** Every job is ffmpeg-bound, and two 4K scans in
parallel finish no sooner while making both unresponsive. Same reasoning as
D-36 for transfers, reached the same way.

**Jobs live in memory only.** The RESULT is a file on disk, which is what
survives a restart; a job record is a view of work in flight. Persisting it
would add a second thing that can disagree with the queue about what
happened. The last `HISTORY` are kept so the workspace can show what it just
produced.

**The command is built here, once.** The workspace shows the equivalent
`mfp stack ...` line for what it is about to run, and the job carries the
one that actually ran. Both come from `equivalent_command`, so the thing
shown and the thing done cannot drift -- which is the whole claim the GUI
makes about itself.
"""

from __future__ import annotations

import queue as queue_module
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from mfp import logs
from mfp.config import AppConfig
from mfp.errors import MfpError, UsageError
from mfp.models import CamelModel
from mfp.captions import resolve_caption_source
from mfp.cues import parse_timecode
from mfp.errors import StackCancelled
from mfp.stack import DEFAULTS, apply_settings, probe_video, run_stack

#: How many finished jobs to keep. A workspace shows the last handful; a
#: hundred is a memory leak with a nicer name.
HISTORY = 50

StackJobState = Literal[
    "QUEUED", "PREPARING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"
]

TERMINAL: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class StackRequest(CamelModel):
    """One run's parameters, in the same vocabulary the CLI uses.

    Every field is the flag of the same name, deliberately: the workspace
    shows the equivalent command, and a field called something else would
    make that translation a place for bugs to live.
    """

    video: str
    subs: str | None = None
    roi: str | None = None
    start: str | None = None
    end: str | None = None
    offset: str | None = None
    sub_lang: str = "orig"
    max_strips: int | None = None
    preview: bool = True
    settings: dict[str, str] = {}
    out: str | None = None


class StackJob(CamelModel):
    id: str
    state: StackJobState = "QUEUED"
    request: StackRequest
    command: list[str] = []
    #: The last thing the run said about itself. Text rather than a
    #: percentage: the phases have no common unit, and a bar that invents one
    #: is a bar that lies (the queue table learned this about muxing).
    message: str | None = None
    output: str | None = None
    preview: str | None = None
    source: str | None = None
    strips: int | None = None
    cues_found: int | None = None
    truncated: int | None = None
    width: int | None = None
    height: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    #: Folder name under `logs/errors/`, so the workspace can offer to open
    #: the evidence rather than describe it.
    error_bundle: str | None = None
    created_at: str
    finished_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def equivalent_command(request: StackRequest) -> list[str]:
    """The `mfp stack` invocation this request means, argument for argument."""
    cmd = ["mfp", "stack", request.video]
    if request.subs:
        cmd += ["--subs", request.subs]
    if request.roi:
        cmd += ["--roi", request.roi]
    if request.start:
        cmd += ["--from", request.start]
    if request.end:
        cmd += ["--to", request.end]
    if request.offset:
        cmd += ["--offset", request.offset]
    if request.sub_lang and request.sub_lang != "orig":
        cmd += ["--sub-lang", request.sub_lang]
    if request.max_strips is not None:
        cmd += ["--max-strips", str(request.max_strips)]
    if request.preview:
        cmd += ["--preview"]
    for key in sorted(request.settings):
        cmd += ["--set", f"{key}={request.settings[key]}"]
    if request.out:
        cmd += ["--out", request.out]
    return cmd


def validate(request: StackRequest) -> None:
    """Everything that can be refused before a thread is involved.

    At the boundary rather than in the worker: a bad `--set` key or an
    unreadable timecode is a 400 the caller can fix, and finding that out
    through a FAILED job thirty seconds later is a worse version of the same
    answer.
    """
    if request.subs and request.roi:
        raise UsageError(
            "captions and a region of interest are different sources; pick one"
        )
    if not request.subs and not request.roi:
        raise UsageError(
            "give a caption source or a region of interest: automatic band "
            "detection is not reliable"
        )
    video = Path(request.video).expanduser()
    if not video.is_file():
        raise UsageError(f"no such video file: {video}")
    for name in ("start", "end", "offset"):
        raw = getattr(request, name)
        if raw:
            parse_timecode(raw)
    if request.max_strips is not None and request.max_strips < 1:
        raise UsageError("maxStrips must be at least 1")
    apply_settings([f"{k}={v}" for k, v in request.settings.items()])
    # Last, because it costs an ffprobe: a file that is not a video fails
    # here rather than after the caller has waited for a worker slot.
    probe_video(video)


class StackJobRunner:
    """A thread that runs one stack job at a time, and a record of them."""

    def __init__(
        self,
        *,
        config: AppConfig,
        on_event: Callable[[str, dict], None] | None = None,
        history: int = HISTORY,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._history = history
        self._jobs: OrderedDict[str, StackJob] = OrderedDict()
        self._pending: queue_module.Queue[str] = queue_module.Queue()
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._dispatch: Callable[[Callable[[], None]], None] = lambda fn: fn()

    # -- lifecycle ---------------------------------------------------------
    def bind_dispatch(self, dispatch: Callable[[Callable[[], None]], None]) -> None:
        """How to get back onto the loop that owns the broadcaster."""
        self._dispatch = dispatch

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="mfp-stack", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._pending.put("")  # wake the thread so it can notice
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    # -- registry ----------------------------------------------------------
    def submit(self, request: StackRequest) -> StackJob:
        validate(request)
        job = StackJob(
            id=uuid.uuid4().hex[:12],
            request=request,
            command=equivalent_command(request),
            created_at=_now(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
        self._pending.put(job.id)
        self._publish(job)
        return job

    def list(self) -> list[StackJob]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, job_id: str) -> StackJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise StackJobNotFound(f"no stack job {job_id}")
        return job

    def cancel(self, job_id: str) -> StackJob:
        job = self.get(job_id)
        if job.state in TERMINAL:
            # Not an error: a stop button pressed as the job finished is a
            # race the user cannot see, and refusing it would be pedantry.
            return job
        with self._lock:
            self._cancelled.add(job_id)
        if job.state == "QUEUED":
            # It never started, so nothing will notice the flag on its own.
            return self._finish(job, "CANCELLED")
        return job

    def _trim(self) -> None:
        finished = [j for j in self._jobs.values() if j.state in TERMINAL]
        while len(finished) > self._history:
            self._jobs.pop(finished.pop(0).id, None)

    # -- the thread --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stopping.is_set():
            job_id = self._pending.get()
            if not job_id or self._stopping.is_set():
                continue
            try:
                job = self.get(job_id)
            except StackJobNotFound:
                continue
            if job_id in self._cancelled:
                self._finish(job, "CANCELLED")
                continue
            try:
                self._execute(job)
            except Exception as exc:  # the thread must not die (worker.py)
                self._finish(
                    job, "FAILED", error_code="unexpected", error_detail=repr(exc)
                )

    def _execute(self, job: StackJob) -> None:
        request = job.request
        video = Path(request.video).expanduser()
        out = (
            Path(request.out).expanduser()
            if request.out
            else video.with_name(video.stem + "-stack.jpg")
        )
        workdir = out.parent / f".{out.stem}-work"

        with logs.action("stack.job", job=job.id, video=str(video),
                         roi=request.roi, subs=request.subs) as payload:
            try:
                self._update(job, state="PREPARING", message="準備中")
                subs = resolve_caption_source(
                    video,
                    subs=request.subs,
                    sub_lang=request.sub_lang,
                    yt_dlp=self._config.binaries.yt_dlp,
                    say=lambda message: self._update(job, message=message),
                )
                opts = dict(DEFAULTS)
                if request.max_strips is not None:
                    opts["max_strips"] = request.max_strips
                opts.update(apply_settings(
                    [f"{k}={v}" for k, v in request.settings.items()]
                ))

                self._update(job, state="RUNNING")
                result = run_stack(
                    video=video,
                    out=out,
                    workdir=workdir,
                    subs=subs,
                    roi=request.roi,
                    start=parse_timecode(request.start) if request.start else 0.0,
                    end=parse_timecode(request.end) if request.end else None,
                    offset=parse_timecode(request.offset) if request.offset else 0.0,
                    preview=request.preview,
                    opts=opts,
                    on_progress=lambda message: self._update(job, message=message),
                    should_cancel=lambda: job.id in self._cancelled,
                )
            except StackCancelled:
                payload["ok"] = False
                payload["cancelled"] = True
                self._finish(job, "CANCELLED", message="已停止")
                return
            except MfpError as exc:
                bundle = logs.write_error_bundle(
                    "stack",
                    code=exc.error_code,
                    detail=str(exc),
                    params=request.model_dump(by_alias=True),
                    command=job.command,
                    stderr=getattr(exc, "log_stderr", None),
                    evidence=getattr(exc, "log_evidence", ()),
                    extra_files=getattr(exc, "log_files", None),
                )
                payload["ok"] = False
                payload["code"] = exc.error_code
                payload["bundle"] = bundle.name if bundle else None
                self._finish(
                    job,
                    "FAILED",
                    error_code=exc.error_code,
                    error_detail=str(exc),
                    error_bundle=bundle.name if bundle else None,
                )
                return

            payload.update(
                source=result.source, strips=result.strips,
                size=f"{result.width}x{result.height}",
            )
            self._finish(
                job,
                "COMPLETED",
                message=None,
                output=str(result.output),
                preview=str(result.preview) if result.preview else None,
                source=result.source,
                strips=result.strips,
                cues_found=result.cues_found,
                truncated=result.truncated,
                width=result.width,
                height=result.height,
            )

    # -- state -------------------------------------------------------------
    def _update(self, job: StackJob, **fields) -> StackJob:
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)
        self._publish(job)
        return job

    def _finish(self, job: StackJob, state: StackJobState, **fields) -> StackJob:
        with self._lock:
            self._cancelled.discard(job.id)
        return self._update(job, state=state, finished_at=_now(), **fields)

    def _publish(self, job: StackJob) -> None:
        if self._on_event is None:
            return
        payload = job.model_dump(by_alias=True, mode="json")
        emit = self._on_event
        self._dispatch(lambda: emit("stack", payload))


class StackJobNotFound(MfpError):
    """No job by that id. Same shape as `task_not_found`, one surface over."""

    error_code = "stack_job_not_found"
    exit_code = None
