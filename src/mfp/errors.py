"""Error taxonomy for media-fetch-pipeline (PSM Batch 1 core §10).

Every operational failure that must be reported to a caller (CLI exit code,
FetchResult.errorCode, Skill-facing JSON) is represented by a subclass of
`MfpError`. Each subclass carries a fixed `error_code` (the wire-format
string used in FetchResult/Manifest JSON) and the process `exit_code`
mapped by PSM §10. `cdp_timeout` has no process exit code of its own: it is
a strategy-level signal that makes the Instagram adapter fall through to
the next strategy in the chain (PSM §5.1) and therefore never reaches the
CLI as a process exit.

Fail-closed (Phase 2 §4.3): every exception here denies the operation it
was raised for. No caller may catch one of these and silently substitute a
best-effort guess.
"""

from __future__ import annotations

from typing import ClassVar


class MfpError(Exception):
    """Base class for every taxonomy error in PSM Batch 1 core §10.

    Not itself one of the ten taxonomy rows; concrete subclasses below are.
    """

    error_code: ClassVar[str] = "unknown_error"
    exit_code: ClassVar[int | None] = 1

    def __init__(self, detail: str | None = None, **context: object) -> None:
        self.detail = detail
        self.context = context
        super().__init__(detail or self.error_code)

    def to_dict(self) -> dict[str, object]:
        """Serialize for the `errorCode` / `errorDetail` pair used in
        FetchResult items and Manifest.degradedReason plumbing."""
        return {
            "errorCode": self.error_code,
            "errorDetail": self.detail,
            **self.context,
        }


class UsageError(MfpError):
    """Argument/usage error at the CLI layer (PSM §4.2 exit 2).

    Not a named row in the §10 taxonomy table -- that table only enumerates
    errorCode values carried in FetchResult items, and a bad CLI invocation
    never reaches that stage. Added as the smallest reasonable completion
    of the exit-code space defined in §4.2; see delivery report assumptions.
    """

    error_code = "usage_error"
    exit_code = 2


class SourceNotFound(UsageError):
    """The caller named a file or folder and it is not there.

    A subclass of `UsageError` rather than a sibling, because that is what it
    still IS: the argument cannot be used, exit 2, HTTP 400, and every
    `pytest.raises(UsageError)` over a missing path stays true. What it is
    not is the sentence `usage_error` renders as -- 「請求格式錯誤：呼叫端送出
    了服務無法解讀的參數」, which tells a user who mistyped a path that the
    PROGRAM sent a malformed request (UX walkthrough 2026-09-07, F1). That is
    the shape D-155 named: an error message may not claim a cause the program
    did not determine, and 「the caller sent something unparseable」 is a
    claim about our own code made over the most ordinary user typo there is.

    `translate.refuse_missing_source` argued the other way when it shipped --
    "one sentence must not have two codes" -- and it was right about the
    premise and wrong about which way to resolve it. There is now ONE code
    for that sentence and it is this one; `usage_error` keeps the requests
    that really are malformed, which is what its wording was written for.
    """

    error_code = "source_not_found"


class UnsupportedUrlError(MfpError):
    """No adapter matched, or URL failed the platform allowlist."""

    error_code = "unsupported_url"
    exit_code = 3


class DependencyMissingError(MfpError):
    """yt-dlp / gallery-dl / ffmpeg / Chrome not found."""

    error_code = "dependency_missing"
    exit_code = 6


class ChromeDefaultProfileError(MfpError):
    """Configured Chrome profile dir resolves to Chrome's default user data
    directory (Chrome 136+ constraint, PSM §5.2)."""

    error_code = "chrome_default_profile"
    exit_code = 6


class CdpTimeoutError(MfpError):
    """A CDP step exceeded its timeout. Strategy-level only: the Instagram
    adapter catches this and falls through to the next strategy (PSM
    §5.1/§5.3). Never surfaces as a process exit code on its own."""

    error_code = "cdp_timeout"
    exit_code = None


class LoginWallError(MfpError):
    """Redirect to login, or a login form was present in the response."""

    error_code = "login_wall"
    exit_code = 4


class RateLimitedError(MfpError):
    """HTTP 429, or a block heuristic fired."""

    error_code = "rate_limited"
    exit_code = 4


class BudgetExhausted(MfpError):
    """Local fetch budget governor cap reached (hourly, per-run, or an
    active cooldown from a prior `report_block`). Name matches PSM §7's
    exact reference: "acquire... raises BudgetExhausted"."""

    error_code = "budget_exhausted"
    exit_code = 7


class UpstreamStructureChange(MfpError):
    """Page loaded successfully but the parser produced zero items --
    signals a suspected upstream markup change (PSM §5.4 step 9). Name
    matches PSM's exact reference: "raise UpstreamStructureChange"."""

    error_code = "upstream_structure_change"
    exit_code = 5


class UpstreamUnreachable(MfpError):
    """The platform never answered, or answered with its own fault.

    Split out of `upstream_structure_change` on 2026-09-03 (D-155), for the
    same reason `no_media_in_post` was split out of it in O-10: exit 5 tells
    a reader "the site changed shape and this needs a code fix", and saying
    that about a DNS failure sends them to debug a parser over a Wi-Fi blip.
    Measured: yt-dlp reports a refused connection as
    `TransportError(...)`, and a platform 5xx as `HTTP Error 503` -- neither
    is a statement about our parser.

    Exit 1, the residual "did not fully succeed" bucket, deliberately: this
    is not a block (exit 4 tells an agent never to loop-retry, and retrying
    later is exactly right here), not a budget stop (exit 7), and not a code
    fix (exit 5). Nothing in the agent surface's exit table has to change,
    because 1 already means "no specific named cause -- read errorCode".
    """

    error_code = "upstream_unreachable"
    exit_code = 1


class NoMediaInPost(MfpError):
    """The post was found and read, and it simply has nothing to download.

    Split out of `upstream_structure_change` on 2026-08-19 (O-10). That code
    means "the site changed and our parser is now wrong", and SKILL.md tells
    an agent exit 5 needs a code fix -- so returning it for an ordinary
    text-only or image-only X post sent readers to debug a parser over a
    tweet that never had a video. Measured on a real link the user supplied:
    yt-dlp answers "No video could be found in this tweet".

    Exit 3, with `unsupported_url`, because they call for the same response:
    nothing here can be fetched, do not retry, do not try a variation.
    """

    error_code = "no_media_in_post"
    exit_code = 3

    #: The post as read, media lists empty, when the adapter got as far as
    #: reading it (a Threads text post). `probe`/`fetch` ignore it -- nothing
    #: to download is their true answer -- and `brief` explains it, because
    #: for a text-only post the words ARE the post. A `models.Manifest`; typed
    #: loosely because `errors` sits below `models` in the import graph.
    text_manifest: object | None = None


class MediaTransferFailedError(MfpError):
    """Per-item media download failure."""

    error_code = "media_transfer_failed"
    exit_code = 1


class PlatformTransferBlocked(MfpError):
    """The platform handed over a manifest and then refused the bytes.

    Distinct from `rate_limited`, which resolves by waiting, and from
    `media_transfer_failed`, which is a transfer that went wrong on the
    way: here the CDN answers 403 to a link the platform itself issued
    seconds earlier, and neither waiting nor retrying changes it.

    Measured 2026-08-18 against YouTube, and the control is what makes it a
    separate code: every format is refused, including through `yt-dlp`
    itself, with `yt-dlp` current and a JavaScript runtime installed --
    including the progressive format this build was already using before
    M10. Exit 4 because "blocked upstream, stop, do not retry" is exactly
    what an agent should do with it (O-11).
    """

    error_code = "platform_transfer_blocked"
    exit_code = 4


class PathEscape(MfpError):
    """The naming sanitizer's resolved output path escaped the configured
    output root. Name matches PSM §8's exact reference: "raise PathEscape".
    """

    error_code = "path_escape"
    exit_code = 1


class LinkExpired(MfpError):
    """A signed CDN URL was already past `expiresAt` when we tried to use it.

    Raised at fetch time, not on a timer: the only moment expiry matters is
    the moment we are about to use the URL (spike-02 §7, measured TTL ~32 h).
    O-5 rules that the GUI answers this by re-probing rather than asking.
    """

    error_code = "link_expired"
    exit_code = 1


class QueueLocked(MfpError):
    """Another live process already holds the queue lock (G6 §5.1).

    Single-instance enforcement in the Electron shell covers two windows of
    the shell. It does not cover the shell running while the user also has a
    standalone `mfp serve` open, and both write the same
    `%APPDATA%/media-fetch-pipeline/queue.json`. Two writers on one JSON file
    is corruption, so the second `serve` refuses to start rather than
    interleaving writes.

    HTTP 409 in `ERROR_STATUS` for taxonomy completeness only: the condition
    is detected before the server binds, so no client ever receives it over
    HTTP. The shell reads it off stderr.
    """

    error_code = "queue_locked"
    exit_code = 1


class OutsideStore(MfpError):
    """An analysis artifact would have landed outside every analysis store.

    `INV-P3`. `--out` may move the store; it may not take an analysis product
    out of one. Two reasons it is refused rather than allowed:

    * the download tree is under the output root, so an unchecked `--out`
      lets analysis output land among manual downloads -- the exact mixing
      D-142 exists to end, and it is not detectable afterwards because
      provenance is not recoverable from a file (`INV-P10`);
    * an artifact outside every store is invisible to `mfp.index`, which
      would then be a partial map that reads as a complete one. That was
      D-136's own objection to indexing at all, and this refusal is what
      answers it.

    Exit 3 with the other "this input cannot be used" failures: the argument
    is wrong, not the environment.
    """

    error_code = "outside_store"
    exit_code = 3


class PathTooLong(MfpError):
    """The resolved output path still exceeds the length cap after the
    degradation ladder has been exhausted (PSM Batch 1 §8.1).

    Raised rather than truncated: a truncated name can collide with another
    post's, and one download silently overwriting another is worse than a
    refusal the user can act on.
    """

    error_code = "path_too_long"
    exit_code = 1


# --- the quote-stack / transcript subsystem ---------------------------------
#
# Moved here from `stack.py` on 2026-08-30, when that module was split into
# `cues` / `captions` / `mediatool` / `stack`. They live with every other
# domain error for the same reason those do: `all_wire_error_codes()` finds
# them by walking `MfpError.__subclasses__()`, so a class in a module nobody
# imported is a code that silently leaves the wire surface. Three of the four
# modules raise these; none of them owns them.


class StackError(MfpError):
    """The input was read fine and holds nothing to stack.

    Exit 3, matching `no_media_in_post` rather than the exit 5 reserved for
    "the site changed shape". A clip with no burned subtitles inside the
    given band is not a parser break, and telling an agent it is would send
    it looking for a code fix that does not exist.

    The two causes worth telling apart have their own subclasses below.
    They are classes rather than a per-raise string because that is what
    makes them visible to `all_wire_error_codes()` -- and therefore to the
    registry test that refuses a code with no HTTP status and no GUI
    presentation. A code nothing can present is a row that renders as
    "unrecognized, please report".
    """

    error_code = "nothing_to_stack"
    exit_code = 3


class StackCancelled(Exception):
    """Somebody pressed stop. Deliberately not an `MfpError`.

    A cancellation is not a failure: it earns no wire error code, no error
    bundle, and no place in the taxonomy. The job surface turns it into a
    state; the CLI cannot raise it at all, having nothing to cancel with.
    """


class NoSubtitlePixelsInBand(StackError):
    """`--roi` names rows that hold no subtitle in this window.

    Either the band is aimed wrong, or the video's subtitles are a separate
    caption track and this is the wrong path entirely. Both are the caller's
    to fix, and the difference is visible in the preview the refusal keeps.
    """

    error_code = "no_subtitle_pixels_in_band"


class MediaToolFailed(StackError):
    """ffmpeg or ffprobe exited non-zero.

    Distinct from `dependency_missing`: the program is installed and ran --
    it refused this particular work. The full stderr goes to the error
    bundle, since the three lines an error message can carry are rarely the
    three that say why.
    """

    error_code = "media_tool_failed"

# Every concrete (non-base, non-UsageError) taxonomy row, keyed by wire
# error_code, for lookup when reconstructing exit codes from a FetchResult
# item's `errorCode` string (e.g. in the CLI's batch-result summarizer).
#
# A row missing here does not fail loudly: `exit_code_for` returns None and
# `pipeline.fetch_exit_code` reads that as `or 1`, so the run reports exit 1
# (partial) whatever the class declared. `platform_transfer_blocked` was
# missing from 2026-08-18 until 2026-08-23, which meant the ONE code whose
# purpose is to stop an agent retrying came back as the code that invites
# one. `test_errors.py::test_every_declared_exit_code_survives_the_round_trip`
# is what now keeps this list honest -- adding a class is no longer enough
# to be counted, and forgetting this list is no longer silent.
TAXONOMY: dict[str, type[MfpError]] = {
    cls.error_code: cls
    for cls in (
        # Exit 2, inherited from `UsageError`, which is itself absent from
        # this table -- a bad CLI invocation has no §10 row to look up. This
        # one is here anyway because the round-trip test's rule is about the
        # LOOKUP, not about the wire: a class that declares an exit code and
        # answers None when asked for it is wrong whether or not anything
        # currently asks.
        SourceNotFound,
        UnsupportedUrlError,
        DependencyMissingError,
        ChromeDefaultProfileError,
        CdpTimeoutError,
        LoginWallError,
        RateLimitedError,
        BudgetExhausted,
        UpstreamStructureChange,
        UpstreamUnreachable,
        NoMediaInPost,
        MediaTransferFailedError,
        PlatformTransferBlocked,
        LinkExpired,
        PathEscape,
        PathTooLong,
        OutsideStore,
        QueueLocked,
        # The quote-stack / transcript family. Rows here since 2026-08-30,
        # when these classes moved out of `stack.py` -- not because they
        # started travelling on a FetchResult (they never have; the fetch
        # path raises none of them), but because `exit_code_for` returning
        # None for a code whose class plainly declares 3 is a lookup that
        # lies. The reflection test in `test_errors.py` is what noticed.
        StackError,
        NoSubtitlePixelsInBand,
        MediaToolFailed,
    )
}

#: Codes emitted by the loopback guard (`server/security.py`), which is
#: middleware and returns a JSONResponse directly rather than raising.
#: Listed here so the wire surface has one place to enumerate, not two.
GUARD_ERROR_CODES: frozenset[str] = frozenset({"forbidden_host", "cross_origin_denied"})

#: Codes the client itself invents when it never reached the server. Never
#: emitted by this process; listed so the GUI's obligation to present them is
#: checkable from here.
CLIENT_ERROR_CODES: frozenset[str] = frozenset({"server_unreachable"})

#: Codes raised by a verb that has no HTTP route, so no client can ever
#: receive them (health check 2026-08-27, `crd_9c22a1`).
#:
#: Not an exception to the wire surface -- a different origin, the same way
#: `GUARD_ERROR_CODES` is. Until Phase U the distinction did not exist:
#: `queue`, `stack` and `transcript` all sit behind one of the four routers
#: `server/app.py` registers, so "every code a module can raise" and "every
#: code a client can receive" were the same set and `all_wire_error_codes()`
#: could walk the subclasses and call the result the wire surface. `brief` is
#: the first verb with no route, and it made the two readings disagree: the
#: walk would have demanded an `ERROR_STATUS` row and a GUI presentation for
#: a code no client can be handed, while `test_the_gui_presents_nothing_that
#: _cannot_happen` would have called that same GUI row a phantom.
#:
#: Membership rule, so the next person is not guessing: a code belongs here
#: **iff** no router in `server/app.py` can reach the code that raises it.
#: Give `brief` an HTTP route and this entry must go, or the GUI will have no
#: presentation for a code it can now receive.
#: Empty since M4 (2026-09-01), when `brief` gained HTTP routes and
#: `analysis_write_failed` stopped being CLI-only. Kept rather than deleted:
#: the membership rule is「this code has no route that can emit it」, and a
#: constant that exists with nothing in it states that fact where the next
#: person adding a CLI-only failure will look.
CLI_ONLY_ERROR_CODES: frozenset[str] = frozenset()


def all_wire_error_codes() -> frozenset[str]:
    """Every `errorCode` string a client can ever receive.

    Three sources -- raised exceptions, the loopback guard, and the client's
    own synthetic codes -- and the point of gathering them is that they used
    to be enumerated in three places that disagreed. `queue.py`'s errors are
    included via the recursive subclass walk, which is why this is a function
    and not a constant: it must be called after the modules that define them
    are imported.

    `CLI_ONLY_ERROR_CODES` is then subtracted, which is the one place this
    function's name and its implementation are made to agree: the walk finds
    everything RAISABLE, and a client receives a subset of that.
    """

    def walk(cls: type[MfpError]) -> set[str]:
        found: set[str] = set()
        for sub in cls.__subclasses__():
            code = sub.__dict__.get("error_code")
            if code:
                found.add(code)
            found |= walk(sub)
        return found

    raisable = walk(MfpError) - set(CLI_ONLY_ERROR_CODES)
    return frozenset(raisable | GUARD_ERROR_CODES | CLIENT_ERROR_CODES)


def exit_code_for(error_code: str) -> int | None:
    """Look up the PSM §10 exit code for a wire-format error_code string.

    Returns None for unknown codes or for taxonomy rows without a process
    exit code (currently only `cdp_timeout`).
    """
    cls = TAXONOMY.get(error_code)
    return cls.exit_code if cls is not None else None
