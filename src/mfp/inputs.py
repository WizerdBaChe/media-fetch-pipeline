"""Paste-anything input pipeline (PSM Batch 2 GUI §5).

Turns arbitrary pasted text into normalized, deduplicated `ParsedItem`s plus
a report of every change made to the user's input.

Two properties are load-bearing and must survive any refactor:

1. **Pure.** No network, no filesystem, no clock, no budget cost. The GUI is
   free to call this on every keystroke, and every test is deterministic.
   Identity of already-queued items is supplied by the caller via
   `known_ids`; this module never reaches into the queue.
2. **Nothing silent.** Every automatic modification is counted in
   `ParseReport` (INV-7). Aggressive cleanup is only acceptable because it
   is fully reported -- a rewrite the user cannot see is a rewrite the user
   cannot correct.

`sourceUrl` preserves exactly what was pasted and is never mutated, so the
GUI can always show the user their own input back.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import Field

from mfp.models import CamelModel

# --- models -----------------------------------------------------------------


class ParsedItem(CamelModel):
    """One recognized post. Not a queue Task -- see PSM Batch 2 §5."""

    platform: str
    post_id: str
    canonical_url: str
    source_url: str
    hint_index: int | None = None


class BlockedItem(CamelModel):
    """A recognized post this machine currently cannot fetch.

    Distinct from `unrecognized`, and the distinction is the whole point: we
    know exactly what this is and where it lives, and we are still not going
    to try. `reason` is a wire code (see `capabilities.py`); the GUI turns
    it into a sentence.
    """

    platform: str
    reason: str
    url: str


class ParseReport(CamelModel):
    """What the pipeline changed, itemized (INV-7)."""

    glued: int = 0
    extracted: int = 0
    tracking_stripped: list[str] = Field(default_factory=list)
    duplicates_in_batch: int = 0
    already_in_queue: int = 0
    unrecognized: list[str] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)


class ParseResult(CamelModel):
    items: list[ParsedItem] = Field(default_factory=list)
    report: ParseReport = Field(default_factory=ParseReport)


# --- step 1: extraction -----------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
# Split point for glued URLs: a scheme that is NOT at position 0.
_GLUE_RE = re.compile(r"(?<=.)(?=https?://)", re.IGNORECASE)

# Trailing characters that cannot end a URL. `)` and `]` are handled
# separately because they legitimately appear inside paths.
_TRAILING_JUNK = ".,;!?\"'"
_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{"}


def _split_glued(raw_match: str) -> list[str]:
    """Split a single whitespace-delimited token on embedded schemes.

    A greedy `\\S+` swallows a second URL pasted with no separator
    (`.../p/ABC/?igsh=xxhttps://...`), silently losing it. Splitting before
    every non-initial `http(s)://` recovers both.
    """
    return [part for part in _GLUE_RE.split(raw_match) if part]


def _strip_trailing_junk(url: str) -> str:
    """Remove trailing punctuation that prose, not the URL, contributed.

    A closing bracket is only stripped when it is unbalanced, so a URL that
    genuinely contains `(x)` survives.
    """
    while url:
        last = url[-1]
        if last in _TRAILING_JUNK:
            url = url[:-1]
            continue
        opener = _BRACKET_PAIRS.get(last)
        if opener is not None and url.count(opener) < url.count(last):
            url = url[:-1]
            continue
        break
    return url


def extract_urls(raw: str) -> tuple[list[str], int, int]:
    """Return (urls, glued_count, extracted_count).

    `glued_count` counts URLs recovered by splitting; `extracted_count`
    counts URLs that had surrounding prose on their line (i.e. the line was
    not just a bare URL).
    """
    urls: list[str] = []
    glued = 0
    extracted = 0

    for line in raw.splitlines():
        line_urls: list[str] = []
        for match in _URL_RE.findall(line):
            parts = _split_glued(match)
            glued += len(parts) - 1
            line_urls.extend(_strip_trailing_junk(p) for p in parts)
        line_urls = [u for u in line_urls if u]
        if line_urls and line.strip() != "".join(line_urls):
            # The line carried prose around (or between) its URLs.
            extracted += len(line_urls)
        urls.extend(line_urls)

    return urls, glued, extracted


# --- step 2: platform identification ----------------------------------------

_IG_HOSTS = {"instagram.com", "www.instagram.com"}
_TH_HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_BILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}

# Instagram serves one post at two spellings: `/p/<code>/`, and -- when you
# open it from a profile, which is how most links get copied -- the longer
# `/<username>/p/<code>/`. Both come out of the address bar, so refusing the
# second one rejects a URL the user can plainly see is a post.
#
# The username segment is dropped in `_canonical_path`, and NOT because of
# INV-1: identity there is `(platform, postId)`, and the shortcode is the
# same in both spellings, so the queue would merge them either way. What
# needs the two to agree is `canonicalUrl`, which is the string `runs.key_for`
# files a run under -- two spellings would be two source keys for one post,
# and an analysis made from one link would not be found from the other.
# (`analyzed` also asks by `canonical_post_key`, which is the belt to this
# braces; `runs.find` on a bare source is not.)
_IG_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]+)/?$")
_TH_POST_RE = re.compile(r"^/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)/?$")
_TH_SHARE_RE = re.compile(r"^/share/([A-Za-z0-9_-]+)/?$")
_YT_PATH_RE = re.compile(r"^/(?:shorts|live|embed)/([A-Za-z0-9_-]+)/?$")
_X_PATH_RE = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)/?$")
_BILI_PATH_RE = re.compile(r"^/video/([A-Za-z0-9]+)/?$")


def _generic_id(host: str, path: str) -> str | None:
    """Provisional identity for a host we have no adapter for (O-10).

    Shaped like the Threads `share:` case: a placeholder that probe time
    rewrites with the id yt-dlp actually reports. We cannot know the real id
    without fetching, and inventing a stable-looking one would be a lie.

    The slug alone is not enough for identity -- `a.com/x/watch` and
    `b.com/y/watch` share a last segment -- so a short digest of the whole
    canonical URL is appended. Without it two unrelated posts would merge into
    one task under INV-1.

    A bare domain with no path returns None. It is far more likely to be a
    home page than a post, and handing yt-dlp a site root is how a paste
    turns into a crawl.
    """
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", segments[-1]).strip("-.")[:40]
    digest = hashlib.sha1(f"{host}{path}".encode()).hexdigest()[:6]
    return f"url:{slug}-{digest}" if slug else f"url:{digest}"


def identify(host: str, path: str, query: dict[str, str]) -> tuple[str, str] | None:
    """Return (platform, post_id), or None when the URL is not a post.

    A host we know but a path shape we do not (a profile page, the feed) is
    NOT a post and returns None -- it must not be handed to yt-dlp, which
    would try to download a whole profile. That guard is about *known*
    platforms and is unchanged by O-10: an unknown host cannot be checked
    that way, so it becomes `generic` and yt-dlp decides.
    """
    if host in _IG_HOSTS:
        m = _IG_PATH_RE.match(path)
        return ("instagram", m.group(2)) if m else None

    if host in _TH_HOSTS:
        m = _TH_POST_RE.match(path)
        if m:
            return ("threads", m.group(2))
        m = _TH_SHARE_RE.match(path)
        # Provisional: the real post id needs a redirect, resolved at probe
        # time from finalUrl (spike-02 §5).
        return ("threads", f"share:{m.group(1)}") if m else None

    if host in _YT_HOSTS:
        if host == "youtu.be":
            slug = path.strip("/")
            return ("youtube", slug) if slug else None
        if path == "/watch" and query.get("v"):
            return ("youtube", query["v"])
        m = _YT_PATH_RE.match(path)
        return ("youtube", m.group(1)) if m else None

    if host in _X_HOSTS:
        m = _X_PATH_RE.match(path)
        return ("x", m.group(2)) if m else None

    if host in _BILI_HOSTS:
        if host == "b23.tv":
            slug = path.strip("/")
            return ("bilibili", f"short:{slug}") if slug else None
        m = _BILI_PATH_RE.match(path)
        return ("bilibili", m.group(1)) if m else None

    # O-10 (ruled 2026-08-12): an unrecognized host goes to `generic` and is
    # delegated to yt-dlp at probe time rather than refused here. Refusing was
    # safe and predictable but made `generic` dead code and threw away the
    # thousand-plus sites yt-dlp already handles.
    generic_id = _generic_id(host, path)
    return ("generic", generic_id) if generic_id else None


# --- step 3: normalization --------------------------------------------------

# Dropped from canonicalUrl. `img_index` is handled separately (step 3c):
# it is removed from identity but preserved as `hintIndex`.
_TRACKING_PARAMS = frozenset(
    {"igsh", "igshid", "xmt", "slof", "si", "feature", "fbclid", "gclid",
     "ref", "ref_src", "ref_url", "hl", "s", "t", "spm_id_from", "vd_source"}
)
_TRACKING_PREFIXES = ("utm_", "_nc_")

# Query params that carry meaning and must survive normalization.
_MEANINGFUL_PARAMS = frozenset({"v"})


def _is_tracking(name: str) -> bool:
    return name in _TRACKING_PARAMS or name.startswith(_TRACKING_PREFIXES)


def _canonical_path(platform: str, host: str, path: str) -> str:
    """Force one spelling per post so `/p/ABC` and `/p/ABC/` are one task."""
    if platform == "instagram" and (m := _IG_PATH_RE.match(path)):
        # `/<username>/p/ABC/` is the same post as `/p/ABC/`; the profile
        # prefix is navigation, not identity.
        return f"/{m.group(1)}/{m.group(2)}/"
    if platform in ("instagram", "threads") and not path.endswith("/"):
        return path + "/"
    if platform in ("youtube", "x", "bilibili"):
        return path.rstrip("/") or "/"
    return path


def normalize(url: str) -> tuple[ParsedItem, list[str]] | None:
    """Normalize one URL. Returns (item, stripped_param_names) or None.

    None means "not a recognizable post"; the caller reports it under
    `unrecognized` rather than guessing.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    host = parts.hostname or ""
    if not host:
        return None
    host = host.lower()

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = {k: v for k, v in query_pairs}

    identified = identify(host, parts.path, query)
    if identified is None:
        return None
    platform, post_id = identified

    hint_index: int | None = None
    raw_hint = query.get("img_index")
    if raw_hint is not None and raw_hint.isdigit():
        hint_index = int(raw_hint)

    stripped = sorted({k for k, _ in query_pairs if _is_tracking(k)})

    kept = [
        (k, v)
        for k, v in query_pairs
        if k in _MEANINGFUL_PARAMS and not _is_tracking(k)
    ]
    canonical_query = "&".join(f"{k}={v}" for k, v in kept)

    canonical = urlunsplit(
        ("https", host, _canonical_path(platform, host, parts.path), canonical_query, "")
    )

    return (
        ParsedItem(
            platform=platform,
            post_id=post_id,
            canonical_url=canonical,
            source_url=url,
            hint_index=hint_index,
        ),
        stripped,
    )


# --- steps 4-6: the pipeline ------------------------------------------------


#: Cap on echoed-back input. Long enough to recognize what you pasted, short
#: enough that pasting a novel does not put a novel in every SSE frame.
_MAX_ECHO_CHARS = 500


def _echo(raw: str) -> str:
    text = raw.strip()
    return text if len(text) <= _MAX_ECHO_CHARS else text[:_MAX_ECHO_CHARS] + "…"


def parse(
    raw: str,
    known_ids: set[tuple[str, str]] | None = None,
    blocked_platforms: dict[str, str] | None = None,
) -> ParseResult:
    """Turn pasted text into deduplicated `ParsedItem`s plus a change report.

    `known_ids` are `(platform, post_id)` pairs already in the caller's
    queue; matches are counted as `already_in_queue` and omitted from
    `items`. Passing None means "nothing is queued yet".

    `blocked_platforms` maps platform -> reason code for what this machine
    cannot currently fetch. It is passed IN rather than computed here, and
    that is load-bearing: deciding it requires running yt-dlp to read its
    version, which would cost this module its purity (see the module
    docstring) and make every keystroke spawn a subprocess. The server
    computes it once from a cached `doctor` report; `capabilities.py` owns
    the rule. Passing None means "nothing is blocked", which is both the
    default and the expected state.

    A blocked item is reported and dropped, never queued -- but only the
    blocked ones. The rest of the paste goes through untouched, because a
    batch of twelve links containing one bad platform is still eleven good
    downloads.
    """
    known = known_ids or set()
    blocked = blocked_platforms or {}
    report = ParseReport()

    urls, report.glued, report.extracted = extract_urls(raw)

    # PSM §5 error paths: text containing no URL at all echoes back, so the
    # GUI can say "沒有找到可用的網址" and keep the paste box intact. Without
    # this the report is empty, the strip renders nothing, and the user's text
    # is cleared -- their input vanishes with no account of why.
    if not urls and raw.strip():
        report.unrecognized.append(_echo(raw))

    seen: set[tuple[str, str]] = set()
    items: list[ParsedItem] = []
    stripped_names: set[str] = set()

    for url in urls:
        normalized = normalize(url)
        if normalized is None:
            report.unrecognized.append(url)
            continue
        item, stripped = normalized
        stripped_names.update(stripped)

        identity = (item.platform, item.post_id)
        if identity in known:
            report.already_in_queue += 1
            continue
        if identity in seen:
            report.duplicates_in_batch += 1
            continue
        seen.add(identity)

        # Checked after dedup so pasting the same blocked link twice says
        # "blocked" once, not twice. Checked before `items` because the
        # whole point is that it never reaches the queue.
        reason = blocked.get(item.platform)
        if reason is not None:
            report.blocked.append(
                BlockedItem(platform=item.platform, reason=reason, url=item.source_url)
            )
            continue

        items.append(item)

    report.tracking_stripped = sorted(stripped_names)
    return ParseResult(items=items, report=report)
