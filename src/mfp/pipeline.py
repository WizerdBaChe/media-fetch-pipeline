"""URL -> Manifest -> files: the orchestration the CLI and the queue share.

`download.py` moves bytes and `adapters/` know their platforms. Neither
knows how to answer "the user handed me four URLs, two of them Instagram,
one unparseable, and the budget ran out on the third". That answer is here,
in one place, because there are two callers of it -- `cli.py` and the queue
worker -- and two copies would disagree about exactly the cases nobody
tests: which failure stops a batch, which exit code a mixed batch earns,
whether a post that never probed appears in the result at all.

Three rules the rest of the module follows from:

1. **A failure that will repeat stops the batch.** `budget_exhausted` and
   the block signals (`rate_limited`, `login_wall`) are statements about the
   next request as much as this one. Continuing would spend a cooldown on
   URLs that cannot succeed, which is the opposite of what the governor is
   for. Everything else is per-URL and the batch continues.
2. **A post that produced nothing still gets a row.** `FetchResult.items`
   cannot represent a URL that failed before it had items, so `posts[]`
   does. Reporting a failure by absence is how a silent failure looks.
3. **The exit code describes the run, not the last thing that happened.**
   §4.2's specific codes are reserved for a run whose failures share one
   cause; a mixed batch is `1`, which is what "partial" means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Collection, Iterable, Literal, Sequence
from urllib.parse import parse_qsl, urlsplit

from mfp.adapters.base import FetchContext, PlatformAdapter
from mfp.budget import FetchBudgetGovernor
from mfp.config import AppConfig
from mfp.download import ProgressCallback, nothing_choosable_detail, plan_destinations
from mfp.errors import (
    BudgetExhausted,
    LoginWallError,
    MfpError,
    RateLimitedError,
    UnsupportedUrlError,
    UsageError,
    exit_code_for,
)
from mfp.inputs import identify
from mfp.models import (
    FetchResult,
    FetchResultBudget,
    FetchResultItem,
    FetchResultPost,
    Manifest,
)
from mfp.policy import Policy

#: Platforms the browser chain serves, and the ones yt-dlp serves. Read from
#: the adapters rather than restated, so adding a platform to an adapter
#: cannot leave the registry behind.
StopReason = Literal["budget_exhausted", "blocked", "user_cancelled"]

#: Errors that end a batch rather than just the URL that raised them. See
#: rule 1 in the module docstring.
_BATCH_STOPPERS: dict[type[MfpError], StopReason] = {
    BudgetExhausted: "budget_exhausted",
    RateLimitedError: "blocked",
    LoginWallError: "blocked",
}

#: Which exit code a stopped batch earns, independent of the per-item rows.
STOP_EXIT_CODES: dict[str, int] = {
    "budget_exhausted": 7,
    "blocked": 4,
    # Not in §4.2's table: the user asking to stop is not one of its seven
    # conditions. `1` is the residual "did not fully succeed" bucket, and
    # `stopReason` in the JSON says which kind.
    "user_cancelled": 1,
}


# --- adapter registry --------------------------------------------------------


def platform_of(url: str) -> str | None:
    """Which platform `url` belongs to, or None if it is not a post.

    Shares `inputs.identify` with the paste box on purpose: a URL the GUI
    accepted and the CLI rejects would be a defect the user cannot explain.
    """
    split = urlsplit(url)
    return (
        identified[0]
        if (identified := identify(split.hostname or "", split.path, dict(parse_qsl(split.query))))
        else None
    )


def build_adapter(
    platform: str, *, state_dir: Path, sessions: object | None = None
) -> PlatformAdapter:
    """The one place a platform name becomes a working adapter.

    Imports are local because the browser chain pulls in a websocket client
    that `mfp doctor` and `mfp serve` have no use for.
    """
    from mfp.adapters.instagram.adapter import PLATFORMS as BROWSER_PLATFORMS
    from mfp.adapters.ytdlp import PLATFORMS as YTDLP_PLATFORMS

    if platform in BROWSER_PLATFORMS:
        from mfp.adapters.instagram.adapter import InstagramAdapter
        from mfp.adapters.instagram.cdp import connect, http_get

        # `sessions` decides who owns the browser's lifetime: None keeps the
        # per-call behaviour every CLI run and test has always had; the queue
        # worker passes a shared one so a batch launches Chrome once.
        return InstagramAdapter(
            state_dir=state_dir,
            connector=connect,
            http_get=http_get,
            sessions=sessions,  # type: ignore[arg-type]
        )

    if platform in YTDLP_PLATFORMS:
        from mfp.adapters.ytdlp import YtDlpAdapter

        return YtDlpAdapter()

    raise UnsupportedUrlError(
        f"no adapter serves platform {platform!r}", platform=platform
    )


AdapterFor = Callable[[str], PlatformAdapter]


def default_adapter_for(state_dir: Path, *, sessions: object | None = None) -> AdapterFor:
    """An `adapter_for` that resolves against the real adapters.

    Every entry point takes an `adapter_for` rather than reaching for this
    directly, so a test can substitute a fake without a browser anywhere
    near it.
    """
    cache: dict[str, PlatformAdapter] = {}

    def resolve(platform: str) -> PlatformAdapter:
        # Cached per platform because a batch of eight Instagram URLs should
        # not build eight adapters. Whether those eight also share one Chrome
        # is `sessions`' business now, not this cache's -- the D-30 stale-port
        # fix is what made repeated sessions safe enough to reuse at all.
        if platform not in cache:
            cache[platform] = build_adapter(
                platform, state_dir=state_dir, sessions=sessions
            )
        return cache[platform]

    return resolve


def build_context(
    config: AppConfig,
    *,
    governor: FetchBudgetGovernor | None = None,
    output_root: str | None = None,
    select: Collection[int] | None = None,
    on_progress: ProgressCallback | None = None,
    allow_silent_video: bool = False,
    audio_language: str | None = None,
) -> FetchContext:
    return FetchContext(
        budget=governor if governor is not None else FetchBudgetGovernor(config),
        config=config,
        output_root=output_root or config.output_root,
        select=select,
        on_progress=on_progress,
        allow_silent_video=allow_silent_video,
        audio_language=audio_language,
    )


# --- probe -------------------------------------------------------------------


@dataclass
class ProbeOutcome:
    """One URL's probe result. `status` mirrors `FetchResultItem.status` so
    the two halves of a run read the same way."""

    url: str
    status: Literal["ok", "failed", "skipped"] = "ok"
    platform: str | None = None
    manifest: Manifest | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.manifest is not None

    def to_row(self) -> dict[str, object]:
        """The `mfp probe --json` row (stdout contract)."""
        return {
            "url": self.url,
            "status": self.status,
            "platform": self.platform,
            "manifest": (
                self.manifest.model_dump(by_alias=True, mode="json")
                if self.manifest is not None
                else None
            ),
            "errorCode": self.error_code,
            "errorDetail": self.error_detail,
        }


@dataclass
class ProbeBatch:
    outcomes: list[ProbeOutcome] = field(default_factory=list)
    stop_reason: StopReason | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "ok": bool(self.outcomes) and all(o.ok for o in self.outcomes),
            "stopReason": self.stop_reason,
            "results": [o.to_row() for o in self.outcomes],
        }


def _stopper_for(exc: MfpError) -> StopReason | None:
    for kind, reason in _BATCH_STOPPERS.items():
        if isinstance(exc, kind):
            return reason
    return None


def probe_urls(
    urls: Sequence[str],
    *,
    ctx: FetchContext,
    adapter_for: AdapterFor,
    force_platform: str | None = None,
    on_start: Callable[[str], None] | None = None,
) -> ProbeBatch:
    """Probe each URL in order, stopping the batch on a repeating failure.

    `force_platform` is `--platform <name>`: it overrides detection, which
    is the point of the flag -- a URL shape this build does not recognise is
    exactly when a user reaches for it.
    """
    batch = ProbeBatch()
    for url in urls:
        if batch.stop_reason is not None:
            batch.outcomes.append(
                ProbeOutcome(
                    url=url,
                    status="skipped",
                    error_detail=f"not attempted: the batch stopped ({batch.stop_reason})",
                )
            )
            continue

        platform = force_platform or platform_of(url)
        if on_start is not None:
            on_start(url)

        if platform is None:
            # A known host with an unknown path shape (a profile, the feed)
            # lands here too, and must: handing it on would download a whole
            # account, which is the crawl D-2 forbids.
            batch.outcomes.append(
                ProbeOutcome(
                    url=url,
                    status="failed",
                    error_code=UnsupportedUrlError.error_code,
                    error_detail=f"{url} is not a recognised post URL",
                )
            )
            continue

        try:
            manifest = adapter_for(platform).probe(url, ctx)
        except MfpError as exc:
            batch.outcomes.append(
                ProbeOutcome(
                    url=url,
                    status="failed",
                    platform=platform,
                    error_code=exc.error_code,
                    error_detail=str(exc)[:500],
                )
            )
            batch.stop_reason = _stopper_for(exc)
            continue

        batch.outcomes.append(
            ProbeOutcome(
                url=url,
                status="ok",
                platform=manifest.source.platform or platform,
                manifest=manifest,
            )
        )
    return batch


# --- manifest files ----------------------------------------------------------


def load_manifests(path: Path) -> list[Manifest]:
    """Read `--manifest <file.json>`, accepting either shape it can have.

    Two files legitimately hold manifests and neither is going away: the
    `manifest.json` written beside downloaded media (a bare Manifest --
    this is the file that makes "re-fetch at a different quality without
    re-probing" work, §4.1) and a redirected `mfp probe --json` (the
    envelope). Accepting one and not the other would make the obvious
    gesture fail for no reason a user could guess.
    """
    # utf-8-sig, not utf-8. SKILL.md tells an agent to redirect
    # `mfp probe --json` to a file and pass it back here, and on Windows the
    # default shells write that redirect as UTF-8 *with a BOM* -- PowerShell
    # 5.1's `>` and `Out-File` both do. Refusing it would make the product's
    # own documented gesture fail for a reason no user could guess, which is
    # the same reason both manifest shapes are accepted below.
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise UsageError(f"cannot read --manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"--manifest {path} is not valid JSON: {exc}") from exc

    if isinstance(raw, dict) and "results" in raw:
        payloads = [
            row["manifest"]
            for row in raw.get("results", [])
            if isinstance(row, dict) and row.get("manifest")
        ]
    elif isinstance(raw, list):
        payloads = raw
    else:
        payloads = [raw]

    if not payloads:
        raise UsageError(f"--manifest {path} contains no manifest")

    try:
        return [Manifest.model_validate(payload) for payload in payloads]
    except Exception as exc:  # pydantic ValidationError, deliberately broad
        raise UsageError(
            f"--manifest {path} is not a schemaVersion 1 manifest: {exc}"
        ) from exc


# --- fetch -------------------------------------------------------------------


def _budget_row(governor: FetchBudgetGovernor, platform: str) -> FetchResultBudget:
    snapshot = governor.snapshot(platform)
    return FetchResultBudget(
        platform=platform,
        requests_used=snapshot.requests_used_hour,
        requests_remaining=snapshot.requests_remaining_hour,
        next_allowed_at=snapshot.next_allowed_at,
    )


def _dry_run_rows(
    manifest: Manifest, policy: Policy, ctx: FetchContext
) -> tuple[list[FetchResultItem], str | None, str | None]:
    """Apply policy and resolve paths without transferring.

    Returns (rows, error_code, error_detail) -- a path that cannot be
    resolved is reported here rather than raised, because the whole value of
    a dry run is learning that before the download rather than during it.
    """
    import shutil

    from mfp.policy import apply_policy_to_manifest

    ffmpeg = ctx.config.binaries.ffmpeg or shutil.which("ffmpeg")
    apply_policy_to_manifest(
        manifest,
        policy,
        mux_available=bool(ffmpeg),
        allow_silent_video=ctx.allow_silent_video,
    )

    try:
        paths = plan_destinations(manifest, output_root=ctx.output_root)
    except MfpError as exc:
        return [], exc.error_code, str(exc)[:500]

    rows: list[FetchResultItem] = []
    for item in manifest.items:
        selected = ctx.select is None or item.index in ctx.select
        rows.append(
            FetchResultItem(
                index=item.index,
                status="skipped",
                path=paths.get(item.index) if selected else None,
                chosen=item.chosen if selected else None,
                post_id=manifest.source.id or None,
                error_code=None if item.chosen or not selected else "dependency_missing",
                # The same sentence the real transfer would give, from the
                # same function: a dry run whose reason differs from the run
                # it predicts is a preview of something else.
                error_detail=(
                    None if item.chosen or not selected else nothing_choosable_detail(item)
                ),
            )
        )
    return rows, None, None


def run_fetch(
    outcomes: Iterable[ProbeOutcome],
    policy: Policy,
    *,
    ctx: FetchContext,
    adapter_for: AdapterFor,
    stop_reason: StopReason | None = None,
    dry_run: bool = False,
    on_post: Callable[[ProbeOutcome], None] | None = None,
) -> FetchResult:
    """Transfer every probed manifest into one batch-level FetchResult.

    `stop_reason` seeds the result with a stop that happened during probing,
    so `mfp fetch` reports "the budget ran out" once rather than reporting a
    successful transfer of the two posts that got in first and staying
    silent about the four that did not.
    """
    items: list[FetchResultItem] = []
    posts: list[FetchResultPost] = []
    budgets: dict[str, FetchResultBudget] = {}

    for outcome in outcomes:
        if not outcome.ok or outcome.manifest is None:
            posts.append(
                FetchResultPost(
                    url=outcome.url,
                    ok=False,
                    platform=outcome.platform,
                    error_code=outcome.error_code,
                    error_detail=outcome.error_detail,
                )
            )
            continue

        manifest = outcome.manifest
        platform = manifest.source.platform or outcome.platform or "unknown"

        if stop_reason is not None:
            posts.append(
                FetchResultPost(
                    url=outcome.url,
                    ok=False,
                    platform=platform,
                    post_id=manifest.source.id,
                    error_detail=f"not attempted: the batch stopped ({stop_reason})",
                )
            )
            continue

        if on_post is not None:
            on_post(outcome)

        if dry_run:
            rows, error_code, error_detail = _dry_run_rows(manifest, policy, ctx)
        else:
            try:
                result = adapter_for(platform).fetch(manifest, policy, ctx)
            except MfpError as exc:
                posts.append(
                    FetchResultPost(
                        url=outcome.url,
                        ok=False,
                        platform=platform,
                        post_id=manifest.source.id,
                        error_code=exc.error_code,
                        error_detail=str(exc)[:500],
                    )
                )
                stop_reason = stop_reason or _stopper_for(exc)
                continue
            rows = [row.model_copy(update={"post_id": manifest.source.id}) for row in result.items]
            error_code = error_detail = None
            stop_reason = stop_reason or result.stop_reason

        items.extend(rows)
        budgets.setdefault(platform, _budget_row(ctx.budget, platform))
        attempted = [row for row in rows if row.status != "skipped"]
        posts.append(
            FetchResultPost(
                url=outcome.url,
                ok=error_code is None
                and (bool(attempted) or dry_run)
                and all(row.status == "ok" for row in attempted),
                platform=platform,
                post_id=manifest.source.id,
                # In a dry run nothing has status "ok", so counting those
                # would report "0 file(s)" for a plan that names three.
                item_count=sum(
                    1 for row in rows if row.status == "ok" or (dry_run and row.path)
                ),
                error_code=error_code,
                error_detail=error_detail,
                # Read AFTER fetch: the adapter is what sets it, when it
                # discovers at policy time that the asked-for rendition is
                # not deliverable on this machine.
                degraded=manifest.degraded,
                degraded_reason=manifest.degraded_reason,
            )
        )

    if not budgets:
        # Nothing transferred, but §4.4 requires the block. Report the
        # platform we would have used, so "you have 118 requests left" is
        # still answerable after a run where every URL failed.
        platform = next(
            (o.platform for o in posts if o.platform), None
        ) or "instagram"
        budgets[platform] = _budget_row(ctx.budget, platform)

    ordered = list(budgets.values())
    return FetchResult(
        ok=bool(posts) and all(post.ok for post in posts) and stop_reason is None,
        output_root=str(ctx.output_root),
        items=items,
        budget=ordered[0],
        budgets=ordered if len(ordered) > 1 else None,
        stop_reason=stop_reason,
        posts=posts,
        dry_run=dry_run,
    )


# --- exit codes (§4.2) -------------------------------------------------------


def _codes_of(result: FetchResult) -> set[str]:
    codes = {post.error_code for post in result.posts if not post.ok and post.error_code}
    codes |= {row.error_code for row in result.items if row.status == "failed" and row.error_code}
    return codes


def fetch_exit_code(result: FetchResult) -> int:
    """PSM §4.2, applied to a whole run.

    A stop reason outranks the per-item rows: "the budget ran out" is the
    thing the caller has to act on, and it is true of the run rather than of
    any one item. Otherwise a run whose failures share one cause reports
    that cause, and a mixed one reports partial success -- §4.2 has no code
    for "several different things went wrong", and inventing one would put a
    number in the table that no consumer knows.
    """
    if result.stop_reason is not None:
        return STOP_EXIT_CODES[result.stop_reason]
    codes = _codes_of(result)
    if not codes:
        return 0
    exits = {exit_code_for(code) or 1 for code in codes}
    return exits.pop() if len(exits) == 1 else 1


def probe_exit_code(batch: ProbeBatch) -> int:
    """The same rule for `mfp probe`, which has no items to be partial about."""
    if batch.stop_reason is not None:
        return STOP_EXIT_CODES[batch.stop_reason]
    codes = {o.error_code for o in batch.outcomes if o.status == "failed" and o.error_code}
    if not codes:
        return 0
    exits = {exit_code_for(code) or 1 for code in codes}
    return exits.pop() if len(exits) == 1 else 1


__all__ = [
    "STOP_EXIT_CODES",
    "AdapterFor",
    "ProbeBatch",
    "ProbeOutcome",
    "build_adapter",
    "build_context",
    "default_adapter_for",
    "fetch_exit_code",
    "load_manifests",
    "platform_of",
    "probe_exit_code",
    "probe_urls",
    "run_fetch",
]
