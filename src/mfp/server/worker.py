"""The queue's transfer worker -- what makes the start button do something.

Until this existed the queue was a state machine with nothing behind it:
`:start` moved a row to PROBING and the row sat there, which is why
`/v1/health` reported `probe`/`download` as false. The flag answers "can the
start button do anything", not "does the module exist", and this module is
what flips it.

**Threading.** The queue is not thread-safe and is not made thread-safe
here. Instead the split is strict: this worker's thread does IO only, and
every queue mutation is posted back to the thread that owns the queue
(`dispatch`, which in the server is `loop.call_soon_threadsafe`). One writer,
no locks around the state, and a `dispatch` that runs inline makes the whole
worker testable without a thread at all.

**A job always probes before it transfers**, including when resuming a
PAUSED task. Two reasons, and the second is the load-bearing one:

* A `Task` cannot hold a Manifest. It carries a flat `variants` list, which
  cannot express a carousel's items-by-variants shape, so there is nothing
  to resume *from* without re-reading the post.
* Signatures rotate. A resumed transfer needs a live URL anyway, and
  `download.py` keys its resume check on the URL *path* precisely so that a
  fresh signature still resumes the same `.part` file. Re-probing is what
  that design was built to make cheap.

**Transfers are serial** (D-36): one at a time, on their own thread.

**Two lanes, not one.** Probing and transferring used to share a single
thread, so pressing 下載 on one row while another was being analysed left it
sitting in 下載中 until the probe finished -- reported by the user, and
visible in the old `_run`, which did both halves back to back. They are
different kinds of work with different constraints: a probe drives a real
browser and is paced by the budget governor (D-33), while a CDN transfer
takes no budget slot at all (D-34) and needs no browser. Splitting them
keeps every rule that was actually load-bearing -- one transfer at a time
(D-36), one queue writer via `dispatch` (D-42), serial navigation -- and
drops the coupling that was only an artifact of sharing a thread.

**The thread must not die.** A worker that raises and exits leaves a queue
whose buttons silently stop working -- the failure this project treats as
worse than a crash. Every job is wrapped, and an unexpected exception fails
that task loudly rather than the worker quietly.
"""

from __future__ import annotations

import queue as queue_module
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from mfp.adapters.base import FetchContext
from mfp.budget import FetchBudgetGovernor, shared_governor
from mfp.captions import ORIGINAL_LANG
from mfp.config import AppConfig
from mfp.download import ProgressEvent, plan_destinations
from mfp.errors import MfpError
from mfp.models import FetchResult, Manifest
from mfp.pipeline import AdapterFor, default_adapter_for, platform_of
from mfp.policy import apply_policy_to_manifest, parse_policy
from mfp.queue import (
    LEGAL_TRANSITIONS,
    Task,
    TaskNotFound,
    TaskProgress,
    TaskQueue,
    TaskState,
)

@dataclass(frozen=True)
class Transfer:
    """A probed job, handed to the transfer lane.

    Carries the `FetchContext` the probe used rather than a fresh one: it is
    the object `request_stop` flips to unwind an in-flight transfer, so a
    second one here would mean Pause changes a row's colour and nothing
    else.
    """

    job: "Job"
    manifest: Manifest
    ctx: FetchContext


#: What the caller asked for. `probe` stops at READY; `start` keeps going.
#: The state machine still takes one legal step at a time -- this is about
#: how many steps one click is worth, which §4.1 calls "make this go".
Intent = Literal["probe", "start"]

#: "Run this on the thread that owns the queue."
Dispatch = Callable[[Callable[[], None]], None]


@dataclass(frozen=True)
class Job:
    """Everything the worker needs, captured at submit time.

    A snapshot rather than a task id, so the worker thread never reads the
    queue. Reading it would need a lock, and a lock around a structure the
    event loop mutates on every request is the thing this design avoids.
    """

    task_id: str
    url: str
    intent: Intent
    policy: str | None = None
    #: `None` inherits the global setting; see `Task.write_subs`.
    write_subs: bool | None = None
    select: tuple[int, ...] | None = None


def _shared_browser(state_dir: Path):
    """A browser held across probes.

    Imported here rather than at module scope for the reason `build_adapter`
    gives: the browser chain pulls in a websocket client that `mfp doctor`
    has no use for, and this module is imported in processes that never
    probe.
    """
    from mfp.adapters.instagram.cdp import connect, http_get
    from mfp.adapters.instagram.sessions import SharedChromeSession
    from mfp.adapters.instagram.adapter import _default_launcher

    return SharedChromeSession(
        state_dir=state_dir,
        connector=connect,
        http_get=http_get,
        launcher=_default_launcher,
    )


class TaskWorker:
    def __init__(
        self,
        *,
        queue: TaskQueue,
        config: AppConfig,
        broadcaster,
        state_dir: Path,
        dispatch: Dispatch | None = None,
        adapter_for: AdapterFor | None = None,
        governor: FetchBudgetGovernor | None = None,
    ) -> None:
        self._queue = queue
        #: The config as it was when this worker was built. Read through the
        #: `_config` property, which prefers a bound provider -- see
        #: `bind_config`.
        self._config_at_build = config
        self._read_config: Callable[[], AppConfig] | None = None
        self._broadcaster = broadcaster
        # Inline by default, and SERIALISED, which the first version was not.
        #
        # The comment here used to say inline was correct "while nothing is
        # threaded -- construction, `drain()`, and every test". That was
        # false: `test_worker_lanes.py` calls `start()`, which runs both
        # lanes, with no dispatch bound. Two threads then ran queue mutations
        # inline and `TaskQueue.save` -- write `queue.json.tmp`, `os.replace`
        # -- collided on Windows with `[WinError 32]` and `[Errno 13]`,
        # killing a worker thread. The module's whole threading design is
        # "one writer"; the default was the one path where that was a hope.
        #
        # `RLock` rather than `Lock` because a dispatched callable may
        # dispatch again on the same thread, which a plain lock would
        # deadlock. The server still rebinds this to
        # `loop.call_soon_threadsafe` at startup, before `start()`, and that
        # remains the real answer -- one THREAD owning the queue, not one
        # mutation at a time.
        self._dispatch_lock = threading.RLock()
        self._dispatch = dispatch or self._dispatch_inline
        #: Held only when this worker built its own adapters. An injected
        #: `adapter_for` owns whatever it opens, and a browser this class
        #: never handed out is not this class's to close.
        self._browser = None
        if adapter_for is not None:
            self._adapter_for = adapter_for
        else:
            self._browser = _shared_browser(state_dir)
            self._adapter_for = default_adapter_for(state_dir, sessions=self._browser)
        self._governor = governor if governor is not None else shared_governor(config)
        self._jobs: queue_module.Queue[Job | None] = queue_module.Queue()
        #: Probed jobs waiting for bytes. Separate from `_jobs` so a transfer
        #: never waits behind a probe (and a probe never waits behind a
        #: transfer); one consumer, so D-36 still holds.
        self._transfers: queue_module.Queue[Transfer | None] = queue_module.Queue()
        self._cancelled: set[str] = set()
        #: The job currently holding a transfer open, so `request_stop` can
        #: reach into it. Serial execution (D-36) is what lets this be one
        #: slot rather than a map.
        self._active: tuple[str, FetchContext] | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._transfer_thread: threading.Thread | None = None

    # --- the settings, as they are NOW ---------------------------------------

    @property
    def _config(self) -> AppConfig:
        """The live settings, not the ones this worker was built with.

        `PUT /v1/config` REBINDS `app.state.config` to a new model rather
        than mutating the old one -- it has to, because a half-applied config
        is worse than a rejected one. A worker holding the object it was
        constructed with therefore reads settings that stopped being true the
        moment the user changed them, and nothing says so: 輸出資料夾 and
        品質 have both behaved that way since the settings panel shipped, and
        the caption default would have joined them.

        `_auto_clear_loop` states the rule this restores -- 「a change in
        Settings must not need a restart」 -- and it is the only place that
        was keeping it, because it re-reads `app.state` every pass.
        """
        read = self._read_config
        return read() if read is not None else self._config_at_build

    def bind_config(self, read: Callable[[], AppConfig]) -> None:
        """Point the worker at wherever the live config lives.

        Called by `create_app` once `app.state` exists, which is why this is
        a second step rather than a constructor argument: the worker is built
        before the app that holds the config it must follow.
        """
        self._read_config = read

    # --- submission (owner thread) ------------------------------------------

    def submit(self, task: Task, intent: Intent) -> None:
        """Queue `task` for work. Called after the route's transition."""
        with self._lock:
            self._cancelled.discard(task.id)
        self._jobs.put(
            Job(
                task_id=task.id,
                url=task.canonical_url,
                intent=intent,
                policy=task.policy,
                write_subs=task.write_subs,
                select=tuple(task.selected_indices) if task.selected_indices else None,
            )
        )

    def request_stop(self, task_id: str) -> None:
        """Ask an in-flight job to stop. Pause and cancel both land here.

        Two things, and the second is the one that actually stops bytes: the
        id is remembered so a job that has not started yet never does, AND
        the running job's context is flipped so a transfer already in
        progress unwinds. `FetchContext.cancel` is read fresh on every chunk
        (`standard_fetch` passes `lambda: ctx.cancel`), but only if someone
        sets it -- a value snapshotted at job start would make Pause a
        button that changes the row's colour and nothing else.

        The route has already made the state change; this only tells the
        transfer to unwind. `_apply` then declines to overwrite a state the
        user chose.
        """
        with self._lock:
            self._cancelled.add(task_id)
            if self._active is not None and self._active[0] == task_id:
                self._active[1].cancel = True

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    @property
    def pending(self) -> int:
        """Work not yet finished, on either lane. A probed job waiting for
        bytes is still pending -- reporting only the probe queue would say
        "nothing left" while transfers were still running."""
        return self._jobs.qsize() + self._transfers.qsize()

    # --- lifecycle -----------------------------------------------------------

    def _dispatch_inline(self, run) -> None:
        """The default dispatch: run it here, but never two at once."""
        with self._dispatch_lock:
            run()

    def bind_dispatch(self, dispatch: Dispatch) -> None:
        """Point queue mutations at the thread that owns the queue.

        Called once, at server startup, when the event loop exists. Calling
        it after `start()` would leave the already-running thread posting
        through the old dispatch, so it is deliberately a startup step
        rather than a setting.
        """
        if self._thread is not None:
            raise RuntimeError("bind_dispatch must be called before start()")
        self._dispatch = dispatch

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="mfp-worker", daemon=True)
        self._transfer_thread = threading.Thread(
            target=self._transfer_loop, name="mfp-worker-transfer", daemon=True
        )
        self._thread.start()
        self._transfer_thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop both lanes, probe first.

        Order matters: the probe lane is the one that FEEDS the transfer
        lane, so stopping it first means no transfer is queued after the
        transfer lane has been told there is no more. Both sentinels go in
        before either join, so the two shutdowns overlap instead of adding
        up.
        """
        probe, self._thread = self._thread, None
        transfer, self._transfer_thread = self._transfer_thread, None
        self._jobs.put(None)
        self._transfers.put(None)
        if probe is not None:
            probe.join(timeout=timeout)
        if transfer is not None:
            transfer.join(timeout=timeout)
        # Belt and braces: the probe lane's own `finally` normally does this,
        # but a lane that never started (or that failed to join) must not
        # leave a browser behind either.
        self._close_browser()

    def _loop(self) -> None:
        """The probe lane."""
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                self._run_guarded(job)
                # The browser is held for as long as there is another post to
                # read and dropped the moment there is not. No timer, so
                # there is no window in which a browser is open because a
                # clock has not fired yet -- "is there more work" is the
                # question an idle timeout was only ever approximating.
                if self._jobs.empty():
                    self._close_browser()
        finally:
            # Whatever ends this lane -- sentinel, or an exception that got
            # past `_run_guarded` -- the browser goes with it. This is the
            # `finally` that M2's zero-orphan acceptance rests on, moved out
            # to where the lifetime now lives.
            self._close_browser()

    def _transfer_loop(self) -> None:
        """The transfer lane. One at a time, which is all D-36 asks."""
        while True:
            transfer = self._transfers.get()
            if transfer is None:
                return
            self._transfer_guarded(transfer)

    def _close_browser(self) -> None:
        if self._browser is not None:
            self._browser.close()

    def drain(self) -> int:
        """Run every queued job on the calling thread, BOTH lanes.

        For tests and for `mfp serve`'s synchronous paths -- not used by the
        running server. The lanes are a threading arrangement, not a
        behavioural one, so draining has to produce the same end state a
        threaded run would: transfers are taken as they appear rather than
        after every probe, which is also the order the threads produce.

        The count is probes, as it always was -- the tests that assert on it
        are asking "did my submitted job run".
        """
        done = 0
        while True:
            probed = False
            try:
                job = self._jobs.get_nowait()
            except queue_module.Empty:
                pass
            else:
                if job is None:
                    self._close_browser()
                    return done
                self._run_guarded(job)
                done += 1
                probed = True

            transferred = self._drain_transfers()
            if not probed and not transferred:
                self._close_browser()
                return done

    def _drain_transfers(self) -> bool:
        """Run whatever is waiting on the transfer lane. True if any ran."""
        ran = False
        while True:
            try:
                transfer = self._transfers.get_nowait()
            except queue_module.Empty:
                return ran
            if transfer is None:
                return ran
            self._transfer_guarded(transfer)
            ran = True

    # --- one job -------------------------------------------------------------

    def _run_guarded(self, job: Job) -> None:
        try:
            self._run(job)
        except MfpError as exc:
            self._fail(job, exc.error_code, str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            self._fail(job, "unknown_error", f"{type(exc).__name__}: {exc}"[:500])

    def _transfer_guarded(self, transfer: Transfer) -> None:
        """Same contract as `_run_guarded`, on the other lane: a transfer
        that raises fails its task loudly rather than ending the lane."""
        try:
            self._transfer(transfer)
        except MfpError as exc:
            self._fail(transfer.job, exc.error_code, str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            self._fail(
                transfer.job, "unknown_error", f"{type(exc).__name__}: {exc}"[:500]
            )

    def _run(self, job: Job) -> None:
        """The probe half. Hands the transfer half to the other lane."""
        platform = platform_of(job.url)
        if platform is None:
            self._fail(job, "unsupported_url", f"{job.url} is not a recognised post URL")
            return

        adapter = self._adapter_for(platform)
        ctx = self._context(job)

        manifest = adapter.probe(job.url, ctx)
        policy = self._policy_for(job)
        apply_policy_to_manifest(
            manifest, policy, mux_available=bool(self._ffmpeg())
        )
        self._mark_ready(job, manifest)

        if job.intent != "start" or self.is_cancelled(job.task_id):
            return

        # DOWNLOADING is set here, by the lane that knows the transfer was
        # actually queued -- not by the transfer lane when it gets round to
        # it. A row that says 可下載 while its bytes are already queued would
        # be a row lying about what the user's click did.
        self._mark_downloading(job, manifest)
        self._transfers.put(Transfer(job=job, manifest=manifest, ctx=ctx))

    def _transfer(self, transfer: Transfer) -> None:
        """The bytes half, on the transfer lane. One at a time (D-36)."""
        job, manifest, ctx = transfer.job, transfer.manifest, transfer.ctx
        adapter = self._adapter_for(platform_of(job.url) or "")
        policy = self._policy_for(job)
        with self._lock:
            # Published before the first byte and under the same lock
            # `request_stop` takes, so a pause arriving at any point during
            # the transfer reaches this context rather than racing past it.
            self._active = (job.task_id, ctx)
            ctx.cancel = job.task_id in self._cancelled
        try:
            result = adapter.fetch(manifest, policy, ctx)
        finally:
            with self._lock:
                self._active = None
        self._finish(job, result)

    def _context(self, job: Job) -> FetchContext:
        ctx = FetchContext(
            budget=self._governor,
            config=self._config,
            output_root=self._config.output_root,
            select=set(job.select) if job.select else None,
            caption_language=self._caption_language_for(job),
        )
        # Items are counted rather than read off `item_index`, because
        # `--select` and the GUI's per-row selection both make the indices
        # sparse: item 5 can be the first one transferred, and reporting
        # "5 done" would be a number the user can see is wrong.
        tally: set[int] = set()
        ctx.on_progress = lambda event: self._on_progress(job, event, tally)
        ctx.cancel = self.is_cancelled(job.task_id)
        return ctx

    def _caption_language_for(self, job: Job) -> str | None:
        """`ORIGINAL_LANG` when this job should save captions, else None.

        The GUI's toggle is on/off and the language is always the one the
        video was SPOKEN in -- there is no per-row language, because naming
        one asks the platform to translate and D-156/P-49 says that has to be
        deliberate rather than a setting somebody left behind.

        `None` here is what the whole download path already reads as 「do not
        ask for captions」 (`FetchContext.caption_language`), so nothing
        downstream needed a new flag.
        """
        wanted = job.write_subs
        if wanted is None:
            wanted = self._config.write_subs
        return ORIGINAL_LANG if wanted else None

    def _policy_for(self, job: Job):
        raw = job.policy or self._config.policy
        try:
            return parse_policy(raw)
        except ValueError:
            # A per-row override the user typed cannot fail the transfer --
            # the global default is a defined answer and the row already
            # shows what it was set to.
            return parse_policy(self._config.policy)

    def _ffmpeg(self) -> str | None:
        from mfp import toolchain

        # See `pipeline._apply_policy`: the copy this program installed for
        # the user counts, and it is not on PATH.
        return self._config.binaries.ffmpeg or toolchain.resolve("ffmpeg")

    # --- posting results back (owner thread) ---------------------------------

    def _apply(
        self,
        task_id: str,
        to_state: TaskState | None,
        *,
        fields: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> None:
        """Update a task on the owning thread, transitioning only if legal.

        An illegal transition here is not a bug: it means the user cancelled
        or paused while the job was running, and their choice outranks the
        worker's. The field updates still apply -- knowing which variant was
        chosen is useful on a cancelled row too.
        """

        def run() -> None:
            try:
                task = self._queue.get(task_id)
            except TaskNotFound:
                return  # the row was removed mid-flight (INV-8 cancelled it)

            for key, value in (fields or {}).items():
                setattr(task, key, value)

            if to_state is not None and to_state in LEGAL_TRANSITIONS.get(task.state, frozenset()):
                self._queue.transition(task_id, to_state, error_code=error_code)

            self._queue.save()
            self._publish(task)

        self._dispatch(run)

    def _publish(self, task: Task) -> None:
        self._broadcaster.publish("task", task.model_dump(by_alias=True, mode="json"))

    def _mark_ready(self, job: Job, manifest: Manifest) -> None:
        chosen = [item.chosen for item in manifest.items if item.chosen is not None]
        expiries = sorted(v.expires_at for v in chosen if v.expires_at)
        self._apply(
            job.task_id,
            "READY",
            fields={
                "author": manifest.source.author,
                "title": manifest.source.caption,
                "variants": [v for item in manifest.items for v in item.variants],
                "chosen": chosen[0] if chosen else None,
                # Q2: only ever copied from the manifest's own flag, never
                # inferred here (D-155) -- the probe already did the one
                # determination this task record is allowed to report.
                "captions_unconfirmed": manifest.captions_unconfirmed,
                # The earliest expiry, not the latest: the post stops being
                # fetchable in one piece as soon as the first link dies.
                "expires_at": expiries[0] if expiries else None,
                "progress": TaskProgress(items_total=len(manifest.items)),
            },
        )
        self._announce_exclusions(job, manifest)

    def _announce_exclusions(self, job: Job, manifest: Manifest) -> None:
        """Say what the source offered that never became a quality option.

        Ruled by the user (M10 4a): filter the defective rows out of the
        queue, but TELL them. Silence here would be the same shape as the
        bug it replaces -- a list quietly different from what the source
        said it had.

        `container_mismatch` is deliberately not reported. It counts the
        duplicate renditions of the container family that was not chosen,
        which cost the user no quality; calling them exclusions would make
        a routine pairing decision look like a loss.
        """
        counts = {
            excluded.reason: excluded.count
            for excluded in manifest.excluded
            if excluded.reason != "container_mismatch" and excluded.count
        }
        if not counts:
            return

        def run() -> None:
            self._broadcaster.publish(
                "notice",
                {
                    "level": "info",
                    "code": "formats_excluded",
                    "detail": None,
                    "taskId": job.task_id,
                    # Structured, never a sentence: the server is English by
                    # contract and the renderer is not (D-60).
                    "excluded": counts,
                },
            )

        self._dispatch(run)

    def _mark_downloading(self, job: Job, manifest: Manifest) -> None:
        """Announce the transfer and record the `.part` file INV-6 removes.

        The engine names its own `.part`, so this recomputes the first one
        from the same planner rather than inventing a path. Without it a
        removed-mid-transfer task leaves an orphan nobody owns and nobody
        resumes.
        """
        part_path: str | None = None
        try:
            planned = plan_destinations(manifest, output_root=self._config.output_root)
            first = min(planned) if planned else None
            part_path = f"{planned[first]}.part" if first is not None else None
        except MfpError:
            # A path this build cannot resolve is the transfer's problem to
            # report, with its own error code. Failing here would report it
            # as a worker fault instead.
            part_path = None

        self._apply(
            job.task_id,
            "DOWNLOADING",
            fields={
                "part_path": part_path,
                "output_dir": str(Path(part_path).parent) if part_path else None,
            },
        )

    def _on_progress(self, job: Job, event: ProgressEvent, tally: set[int]) -> None:
        """Progress is broadcast, not persisted.

        `_ProgressPump` already throttles these, but writing `queue.json` on
        every sample would still be a file write per second per task for no
        benefit: INV-4 demotes a live task to PAUSED on load, so a persisted
        byte count describes a transfer that is no longer running.

        `bytes_*` are **per item**, not per post -- each item gets its own
        pump, so the counter restarts at every carousel slot. Measured on a
        live 7-item post: the frames read 456991, 0, 405220, 0, ... Without
        `items_done` advancing alongside, the row shows a bar that resets
        seven times against a counter stuck at zero, and there is no way to
        tell that from a transfer going in circles.
        """
        tally.add(event.item_index)
        progress = TaskProgress(
            bytes_done=event.bytes_done,
            bytes_total=event.bytes_total,
            bytes_per_sec=event.bytes_per_sec,
            eta_seconds=event.eta_seconds,
            phase=event.phase,
            # Everything seen before the one now running has finished.
            items_done=len(tally) - 1,
        )

        def run() -> None:
            try:
                task = self._queue.get(job.task_id)
            except TaskNotFound:
                return
            progress.items_total = task.progress.items_total
            task.progress = progress
            self._publish(task)

        self._dispatch(run)

    def _finish(self, job: Job, result: FetchResult) -> None:
        done = sum(1 for row in result.items if row.status == "ok")
        first_ok = next((row for row in result.items if row.status == "ok" and row.path), None)
        failed = next((row for row in result.items if row.status == "failed"), None)

        # Carry the byte totals into the finished row rather than zeroing
        # them. Measured on a live fetch: the last DOWNLOADING frame said
        # 10278051/10278051 and the COMPLETED frame that replaced it said
        # 0/None, so the row a user actually ends up looking at is the one
        # that has forgotten how big the download was.
        transferred = sum(row.size_bytes or 0 for row in result.items if row.status == "ok")
        fields: dict[str, object] = {
            "part_path": None,
            "progress": TaskProgress(
                items_done=done,
                items_total=len(result.items),
                bytes_done=transferred,
                bytes_total=transferred or None,
            ),
        }
        if first_ok is not None and first_ok.path:
            fields["output_dir"] = str(Path(first_ok.path).parent)

        if result.stop_reason == "user_cancelled":
            # The route already moved the row to PAUSED or CANCELLED. Naming
            # a state here would either lose that or be refused as illegal;
            # the fields are still worth recording.
            self._apply(job.task_id, None, fields=fields)
            return

        if result.ok:
            self._apply(job.task_id, "COMPLETED", fields=fields)
            return

        code = (
            failed.error_code
            if failed is not None and failed.error_code
            else (result.stop_reason or "media_transfer_failed")
        )
        self._apply(job.task_id, "FAILED", fields=fields, error_code=code)
        self._notice(job, code, failed.error_detail if failed else None)

    def _fail(self, job: Job, code: str, detail: str) -> None:
        self._apply(
            job.task_id,
            "FAILED",
            fields={"progress": TaskProgress()},
            error_code=code,
        )
        self._notice(job, code, detail)

    def _notice(self, job: Job, code: str, detail: str | None) -> None:
        """Say it on the event stream as well as on the row.

        A row that turns red carries an error code and nothing else. The
        notice is where the sentence goes -- and G4's R5-12 finding was that
        saying what happened without saying what to do about it is only
        half a report.
        """

        def run() -> None:
            self._broadcaster.publish(
                "notice",
                {
                    "level": "error",
                    "code": code,
                    # `detail` only -- never a fallback sentence. The
                    # renderer names the code through the same table the row
                    # uses, so a failure cannot be described two ways.
                    "detail": detail,
                    "taskId": job.task_id,
                },
            )

        self._dispatch(run)


__all__ = ["Dispatch", "Intent", "Job", "TaskWorker"]
