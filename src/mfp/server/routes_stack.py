"""HTTP surface for `stack` jobs, one frame at a time, and the run log.

Three groups of routes, all serving the same workspace:

* the jobs themselves -- create, watch, stop, and read back the image;
* `:command`, which answers "what would this run", so the workspace can show
  the equivalent CLI line without a second implementation of it in
  TypeScript that would drift from the one that runs;
* `:frame`, which is what makes the region of interest draggable: the picker
  needs a real frame with its real dimensions, and the M2 acceptance run is
  the argument for it -- `--roi 1300:1545` was carried over from another
  video and landed across a speaker's chest, which nobody could have known
  without seeing it.

The log routes live here rather than in `routes_config` because what they
serve is not configuration: `log_retention_days` is a setting, and the files
are evidence.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from mfp import logs
from mfp.errors import MfpError, UsageError
from mfp.models import CamelModel
from mfp.server.stack_jobs import (
    StackJob,
    StackRequest,
    equivalent_command,
)
from mfp.captions import caption_sidecars
from mfp.cues import parse_timecode
from mfp.stack import grab_frame, probe_video

#: The picker does not need 4K to aim a band, and 4K is 25 MB of base64.
FRAME_MAX_WIDTH = 960


class CommandPreview(CamelModel):
    command: list[str]


class FrameRequest(CamelModel):
    video: str
    #: Seconds, or any timecode the CLI accepts (`1:30`, `00:01:30.5`).
    at: str = "0"
    max_width: int = FRAME_MAX_WIDTH


class FrameResponse(CamelModel):
    """One frame, plus the numbers a drag needs to become a pixel row.

    `frameHeight` is the video's own height -- the value `--roi` is
    expressed in -- while `imageHeight` is what the browser will lay out.
    Sending both is what lets the picker convert without knowing how the
    server scaled anything.
    """

    frame_width: int
    frame_height: int
    image_width: int
    image_height: int
    duration: float
    at: float
    image: str


class SourcesRequest(CamelModel):
    """A file, or a folder to look in. A queue row knows only its output
    directory, and a post can have put more than one video in it."""

    path: str


class StackSource(CamelModel):
    path: str
    width: int
    height: int
    duration: float
    #: Caption files already sitting beside it. Their presence is what lets
    #: the workspace open on the right half of the fork instead of on the
    #: band picker, which is the wrong path for most downloads.
    captions: list[str]


class SourcesResponse(CamelModel):
    sources: list[StackSource]


class LogsResponse(CamelModel):
    log_dir: str
    error_dir: str
    files: int
    file_bytes: int
    bundles: int
    bundle_bytes: int
    retention_days: int
    lines: list[dict]


class SweepResponse(CamelModel):
    files: int
    bundles: int
    bytes_freed: int
    #: True when today's file was deliberately spared. The automatic sweep
    #: always spares it; the manual clear never does, and a button that
    #: quietly left something behind would be the worse of the two.
    kept_today: bool = False


def _runner(request: Request):
    runner = getattr(request.app.state, "stack_runner", None)
    if runner is None:  # pragma: no cover - wired in create_app
        raise UsageError("this build has no stack runner")
    return runner


def build_stack_router() -> APIRouter:
    router = APIRouter(tags=["stack"])

    @router.post("/stack", response_model=StackJob, status_code=201)
    async def create_job(request: Request, body: StackRequest) -> StackJob:
        return _runner(request).submit(body)

    @router.get("/stack", response_model=list[StackJob])
    async def list_jobs(request: Request) -> list[StackJob]:
        return _runner(request).list()

    @router.get("/stack/{job_id}", response_model=StackJob)
    async def get_job(request: Request, job_id: str) -> StackJob:
        return _runner(request).get(job_id)

    @router.post("/stack/{job_id}:cancel", response_model=StackJob)
    async def cancel_job(request: Request, job_id: str) -> StackJob:
        return _runner(request).cancel(job_id)

    @router.get("/stack/{job_id}/image")
    async def job_image(
        request: Request,
        job_id: str,
        kind: str = Query(default="result", pattern="^(result|preview)$"),
    ) -> FileResponse:
        """The image this job produced.

        By job id and never by path: the caller may not name a file for the
        server to read back to it, whatever else it can reach on this
        machine.
        """
        job = _runner(request).get(job_id)
        target = job.output if kind == "result" else job.preview
        if not target or not Path(target).is_file():
            raise UsageError(f"job {job_id} has no {kind} image")
        return FileResponse(target, media_type="image/jpeg")

    @router.post("/stack:command", response_model=CommandPreview)
    async def preview_command(body: StackRequest) -> CommandPreview:
        """What this request would run, without running it.

        Pure and cheap on purpose: the workspace calls it as the form
        changes, so the line it shows is always the server's own rendering
        rather than a copy maintained in the client.
        """
        return CommandPreview(command=equivalent_command(body))

    @router.post("/stack:sources", response_model=SourcesResponse)
    async def sources(body: SourcesRequest) -> SourcesResponse:
        """Which videos here can be stacked, and what captions they have.

        ffprobe decides what is a video, not the extension -- the same rule
        `stack` follows when a post hands back an audio rendition under a
        name that looks like a film.
        """
        target = Path(body.path).expanduser()
        if target.is_dir():
            candidates = sorted(p for p in target.iterdir() if p.is_file())
        elif target.is_file():
            candidates = [target]
        else:
            raise UsageError(f"no such file or folder: {target}")

        found: list[StackSource] = []
        for candidate in candidates:
            try:
                width, height, duration = probe_video(candidate)
            except MfpError:
                continue  # not a video: nothing to say about it
            found.append(
                StackSource(
                    path=str(candidate),
                    width=width,
                    height=height,
                    duration=duration,
                    captions=[str(p) for p in caption_sidecars(candidate)],
                )
            )
        return SourcesResponse(sources=found)

    @router.post("/stack:frame", response_model=FrameResponse)
    async def frame(body: FrameRequest) -> FrameResponse:
        video = Path(body.video).expanduser()
        if not video.is_file():
            raise UsageError(f"no such video file: {video}")
        width, height, duration = probe_video(video)
        at = min(max(0.0, parse_timecode(body.at)), max(0.0, duration - 0.05))
        scale = min(1.0, max(64, body.max_width) / width)
        with tempfile.TemporaryDirectory(prefix="mfp-frame-") as tmp:
            shot = Path(tmp) / "frame.jpg"
            grab_frame(video, at, shot, max_width=int(width * scale))
            payload = base64.b64encode(shot.read_bytes()).decode("ascii")
        return FrameResponse(
            frame_width=width,
            frame_height=height,
            image_width=round(width * scale),
            image_height=round(height * scale),
            duration=duration,
            at=at,
            image=f"data:image/jpeg;base64,{payload}",
        )

    # --- the run log ------------------------------------------------------

    @router.get("/logs", response_model=LogsResponse)
    async def read_logs(request: Request, limit: int = Query(default=200, le=2000)):
        usage = logs.usage()
        return LogsResponse(
            **usage,
            retention_days=request.app.state.config.log_retention_days,
            lines=logs.tail(limit),
        )

    @router.post("/logs:clear", response_model=SweepResponse)
    async def clear_logs() -> SweepResponse:
        report = logs.clear_action_logs()
        return SweepResponse(**report.to_payload())

    @router.post("/logs:clearErrors", response_model=SweepResponse)
    async def clear_errors() -> SweepResponse:
        report = logs.clear_error_bundles()
        return SweepResponse(**report.to_payload())

    return router
