"""What this machine can actually acquire right now, and why not.

`doctor` answers "is the dependency there". This module answers the question
the user is really asking when they paste a link: **will this one work?**
The two are not the same, and the gap between them is what produced O-11 --
a machine that passed every check and still could not transfer a single
YouTube byte.

The one rule here: a platform is blocked only when a condition was
**measured** to break it. A guess costs the user a working download, and a
block that is not true is worse than no block at all, because it cannot be
argued with.

Reason codes are wire values. The GUI maps them to Chinese; nothing here
writes prose a human reads (D-60).
"""

from __future__ import annotations

from mfp.doctor import DoctorReport

#: yt-dlp is present but older than `deps.lock.json`'s `ytDlp.minVersion`.
YTDLP_BELOW_MINIMUM = "ytdlp_below_minimum"

#: yt-dlp could not be found or could not report a version at all.
YTDLP_UNUSABLE = "ytdlp_unusable"

#: Platforms that stop working on a yt-dlp below the floor.
#:
#: Measured 2026-08-19 on one machine, crossing both variables that were
#: previously tested only in isolation:
#:
#:   yt-dlp 2026.07.04 + node reachable  -> HTTP 403 on every format
#:   yt-dlp 2026.08.18 + no JS runtime   -> full DASH ladder transfers
#:
#: So the floor is the yt-dlp version, and a JavaScript runtime is NOT the
#: thing that decides it. Instagram, Threads, X and Bilibili were never
#: affected -- the refusal came from googlevideo, not from yt-dlp -- so
#: this set is deliberately just YouTube rather than "everything yt-dlp
#: touches". Widening it without a measurement would block working
#: downloads.
YTDLP_FLOOR_PLATFORMS: frozenset[str] = frozenset({"youtube"})


def blocked_platforms(report: DoctorReport) -> dict[str, str]:
    """Map platform -> reason code for everything that cannot work now.

    An empty dict means nothing is blocked, which is the expected state on
    a correctly set-up machine. Callers must treat it as the common case:
    the block exists to be absent.
    """
    ytdlp = next((check for check in report.checks if check.name == "yt-dlp"), None)
    if ytdlp is None:
        # No yt-dlp check in the report at all. That is a malformed report,
        # not evidence that a platform is broken, and inventing a block
        # from it would fail closed on a machine that works fine.
        return {}

    if not ytdlp.ok or ytdlp.version is None:
        return {platform: YTDLP_UNUSABLE for platform in YTDLP_FLOOR_PLATFORMS}

    if ytdlp.version_status == "below_minimum":
        return {platform: YTDLP_BELOW_MINIMUM for platform in YTDLP_FLOOR_PLATFORMS}

    # "unpinned", "ok", "unknown", None -- none of these is a measurement
    # that something is broken. `unknown` in particular means the version
    # string could not be compared, and refusing to work because a
    # comparison failed would punish the user for our parser.
    return {}
