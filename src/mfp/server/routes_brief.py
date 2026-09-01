"""HTTP surface for 貼文解說 -- the tool half of `brief`, and only that half.

**Nothing here runs a model** (D-88, `INV-P8`), and ruling R6 is why the
route can exist at all: the desktop prepares the package, shows the pictures
and takes an explanation back, and the looking is done by an agent somewhere
else. A route that called a model would need a provider, a key and a vendor
choice -- all three on `concept-post-brief`'s 明確不做 list.

Its own module rather than a section of `routes_transcript`: the two share no
engine, no progress stream and no blocking-work reasoning. What they do share
is `brief.fetch_package`, which is the point of extracting it -- the CLI and
this router run the same order of operations rather than two that drift.

Two routes:

* `POST /v1/brief` -- probe, open an analysis run, fetch the images. Costs
  platform budget unless the post is already on disk, in which case it costs
  nothing and says so (`reused`).
* `POST /v1/brief:save` -- append an explanation. Appending, never
  rewriting (INV-B4), and refusing any directory outside an analysis store
  (INV-B5), because the value arrives from a caller that has just read an
  untrusted caption.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from mfp import brief as brief_module
from mfp import logs, runs
from mfp.errors import UsageError
from mfp.models import BriefPackage, CamelModel
from mfp.pipeline import default_adapter_for
from mfp.serve import default_queue_path


class BriefRequest(CamelModel):
    url: str
    #: `content` (what the post says) or `visual` (how it looks). Recorded,
    #: never guessed -- the caller classifies from the user's question.
    lane: str | None = None
    refresh: bool = False
    #: Transfer the post's video(s) too, so 逐字稿 can be run over them.
    #: Still no model here (`INV-P8`): fetching a file and reading a file are
    #: different verbs, and this route only does the first.
    with_video: bool = False


class BriefSaveRequest(CamelModel):
    #: The analysis run, exactly as `post.postDir` reported it.
    post: str
    body: str
    lane: str | None = None
    question: str | None = None


class BriefSaved(CamelModel):
    analysis_path: str
    entries: int


def _adapter_for():
    return default_adapter_for(default_queue_path().parent)


def _lane(config, requested: str | None) -> str:
    lane = requested or config.brief.lane_default
    if lane not in brief_module.LANES:
        raise UsageError(
            f"unknown lane {lane!r}; expected one of {', '.join(brief_module.LANES)}"
        )
    return lane


def _run_dir_within_store(candidate: str, output_root: str) -> Path:
    """Resolve the run folder, refusing anything outside an analysis store.

    Fails CLOSED, the same rule and for the same reason as the CLI's
    `--post`: this value can be chosen by whatever read the caption, and the
    failure mode of getting it wrong is writing chosen text to a chosen path.
    Legacy store roots are accepted so an explanation can still be added to an
    analysis made before D-142 renamed the tree.
    """
    from mfp.naming import manifest_filename

    roots = [Path(root).resolve() for root in runs.store_roots(output_root)]
    post_dir = Path(candidate).expanduser().resolve()
    if not post_dir.is_dir():
        raise UsageError(f"no such analysis run: {candidate}")
    if not any(root in post_dir.parents for root in roots):
        raise UsageError(
            f"{candidate} is outside the analysis store, or is a store root "
            "itself. Use the postDir value `brief` reported."
        )
    if not (post_dir / manifest_filename()).is_file():
        raise UsageError(
            f"{candidate} has no {manifest_filename()}, so it is not a post "
            "this tool fetched."
        )
    return post_dir


def build_brief_router() -> APIRouter:
    router = APIRouter(tags=["brief"])

    @router.post("/brief", response_model=BriefPackage)
    async def make_brief(request: Request, body: BriefRequest) -> BriefPackage:
        config = request.app.state.config
        package = brief_module.fetch_package(
            config,
            body.url,
            adapter_for=_adapter_for(),
            lane=_lane(config, body.lane),
            refresh=body.refresh,
            with_video=body.with_video,
        )
        logs.annotate(
            images=len(package.images),
            videos=len(package.videos),
            skipped=len(package.skipped),
            reused=package.reused,
        )
        return package

    @router.post("/brief:save", response_model=BriefSaved)
    async def save_brief(request: Request, body: BriefSaveRequest) -> BriefSaved:
        config = request.app.state.config
        lane = _lane(config, body.lane)
        post_dir = _run_dir_within_store(body.post, config.output_root)

        text = body.body.strip()
        if not text:
            # An empty explanation is not an entry. Writing one would put a
            # dated heading with nothing under it into a file whose whole
            # purpose is that entries are never rewritten.
            raise UsageError("there is nothing to save: the explanation is empty")

        brief_module.save_entry(
            post_dir, lane=lane, body=text, question=body.question
        )
        path = brief_module.analysis_path(post_dir, lane)
        return BriefSaved(
            analysis_path=str(path), entries=len(brief_module.read_entries(path))
        )

    return router
