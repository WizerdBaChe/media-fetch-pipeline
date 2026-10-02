"""X: yt-dlp for the video, the browser for the words.

yt-dlp's X extractor produces a manifest only when the post carries video. For
a text post it exits 1 with "No video could be found in this tweet", which
`classify_failure` correctly maps to `no_media` -- so the answer for `fetch`
was always right and the answer for `brief` was always empty: the words ARE a
text post, and nothing read them (docs/proposal-x-page-read-2026-10-01.md).

This adapter adds the rung Instagram and Threads already have, and only that:

    yt-dlp --video found-----------------> unchanged
       |
       `-- no_media, and the caller wants the words (ctx.read_post_text)
             `-- real Chrome, logged out, one page read
                   |-- HTTP 403 / 429 ----> RateLimitedError
                   |-- a login form ------> LoginWallError
                   |-- focal post absent -> UpstreamStructureChange
                   `-- focal post read ---> NoMediaInPost carrying the text

The last line is deliberate. The post WAS read, and `fetch`/`probe` are still
told the true thing -- nothing to download, exit 3 -- while `brief` takes the
manifest that rides on the error (the Threads mechanism, 2026-09-22).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from mfp import logs
from mfp.adapters import ytdlp
from mfp.adapters.base import FetchContext, PlatformAdapter
from mfp.adapters.instagram.adapter import _default_launcher
from mfp.adapters.instagram.chrome import Connector, Launcher
from mfp.adapters.instagram.extract import looks_like_login_wall
from mfp.adapters.instagram.sessions import ChromeSessions, PerCallChromeSessions
from mfp.adapters.x_extract import (
    XArticle,
    author_chain,
    locate_focal,
    read_articles,
    reply_counts,
    status_time,
)
from mfp.errors import (
    LoginWallError,
    NoMediaInPost,
    RateLimitedError,
    UpstreamStructureChange,
)
from mfp.inputs import identify
from mfp.models import FetchResult, Manifest, ManifestSource, ThreadSegment
from mfp.policy import Policy

PLATFORM = "x"

#: Runs in the page before the read. X renders client-side, so `load` fires
#: before the posts exist; the script waits for them (and for the count to stop
#: changing), then clicks the control that clips a long post. The control is
#: found by structure -- a button inside the post's own words -- because its
#: label is localised (「顯示更多」) and a label match would silently do
#: nothing on any other language (P-77). It expands in place: measured 8/8,
#: same URL, no navigation, no login. Total ceiling 9.5 s, under
#: `chrome.SETTLE_TIMEOUT_S`.
#:
#: The HTTP status comes from the navigation entry, because `outerHTML` cannot
#: say whether a page was served or refused.
READ_JS = """(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const count = () => document.querySelectorAll('article').length;
  let waited = 0;
  while (count() === 0 && waited < 6000) { await sleep(250); waited += 250; }
  let last = -1, stable = 0;
  while (stable < 4 && waited < 8000) {
    const n = count();
    stable = n === last ? stable + 1 : 0;
    last = n;
    await sleep(250); waited += 250;
  }
  const buttons = [...document.querySelectorAll('article div[dir=auto] button[type=button]')];
  buttons.forEach((b) => b.click());
  if (buttons.length) await sleep(1500);
  const nav = performance.getEntriesByType('navigation')[0];
  return JSON.stringify({
    status: nav && nav.responseStatus ? nav.responseStatus : null,
    articles: count(),
    clicked: buttons.length,
  });
})()"""

_BLOCK_STATUSES = frozenset({403, 429})


def _post_id_of(url: str) -> str | None:
    split = urlsplit(url)
    identified = identify(split.hostname or "", split.path, {})
    if identified is None or identified[0] != PLATFORM:
        return None
    return identified[1]


def _settled(raw: str | None) -> dict:
    """The script's own report. A malformed one is `{}`, never a crash."""
    try:
        value = json.loads(raw or "")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class XAdapter(PlatformAdapter):
    name = "x"

    def __init__(
        self,
        *,
        state_dir: Path,
        connector: Connector,
        http_get: Callable[[str], str],
        launcher: Launcher | None = None,
        ytdlp_adapter: ytdlp.YtDlpAdapter | None = None,
        sessions: ChromeSessions | None = None,
    ) -> None:
        self._ytdlp = ytdlp_adapter or ytdlp.YtDlpAdapter()
        self._sessions = sessions or PerCallChromeSessions(
            state_dir=state_dir,
            connector=connector,
            http_get=http_get,
            launcher=launcher or _default_launcher,
        )

    def matches(self, url: str) -> bool:
        return _post_id_of(url) is not None

    def probe(self, url: str, ctx: FetchContext) -> Manifest:
        try:
            return self._ytdlp.probe(url, ctx)
        except NoMediaInPost as no_video:
            if not ctx.read_post_text:
                # `fetch` and `probe`: nothing to download is the whole answer,
                # and a page read would cost a browser for no change in it.
                raise
            return self._read_text_post(url, ctx, no_video)

    def fetch(self, manifest: Manifest, policy: Policy, ctx: FetchContext) -> FetchResult:
        return self._ytdlp.fetch(manifest, policy, ctx)

    # -- the browser rung -------------------------------------------------------

    def _read_text_post(
        self, url: str, ctx: FetchContext, no_video: NoMediaInPost
    ) -> Manifest:
        post_id = _post_id_of(url)
        if post_id is None:  # `matches` makes this unreachable; say so if not
            raise no_video
        ctx.budget.acquire(PLATFORM)

        with self._sessions.session(ctx.config) as session:
            page = session.read_page(url, settle_js=READ_JS)
        report = _settled(page.settle_result)
        status = report.get("status")

        if status in _BLOCK_STATUSES:
            raise RateLimitedError(
                f"x.com answered HTTP {status} to the page read for {url}: it refused "
                "the request rather than failing to answer it -- the same URL usually "
                "works later.",
                url=url,
            ) from no_video

        articles = read_articles(page.html)
        focal = locate_focal(articles, post_id)
        if focal is None:
            if looks_like_login_wall(page.html):
                raise LoginWallError(
                    f"{url} served a login form. D-2 forbids logging in, so this is a "
                    "stop signal: the logged-out read that measured 200 with the whole "
                    "thread on 2026-10-02 no longer works for this post.",
                    url=url,
                ) from no_video
            raise UpstreamStructureChange(
                f"the page for {url} loaded (HTTP {status}) with {len(articles)} "
                f"post(s) on it, none of them post {post_id}. Whether the post is "
                "gone, hidden or the page's structure moved is not determined.",
                url=url,
            ) from no_video

        chain = author_chain(articles, focal)
        manifest = self._manifest(url, post_id, articles, chain)

        clipped = sum(1 for article in chain if article.clipped)
        logs.annotate(
            xPage=(
                f"status={status} articles={len(articles)} chain={len(chain)} "
                f"clicked={report.get('clicked')} stillClipped={clipped}"
            )
        )
        if clipped:
            print(
                f"x: {clipped} of {len(chain)} post(s) are still clipped after the "
                "page's own expand control was clicked; their text is incomplete",
                file=sys.stderr,
            )

        # Pictures and video are not read by this rung. A manifest that said
        # `images: []` for a post the page shows pictures on would be wrong by
        # omission, so the words ride out only when the whole chain is text.
        pictures = sum(article.media for article in chain)
        if pictures:
            raise NoMediaInPost(
                f"{url} shows {pictures} picture/video node(s) this build does not "
                "read from the page; yt-dlp found no video in it.",
                url=url,
            ) from no_video

        error = NoMediaInPost(
            f"{url} was read successfully and contains nothing this tool can "
            f"download: {no_video}",
            url=url,
        )
        error.text_manifest = manifest
        raise error from no_video

    @staticmethod
    def _manifest(
        url: str, post_id: str, articles: list[XArticle], chain: list[XArticle]
    ) -> Manifest:
        focal = chain[0]
        segments: list[ThreadSegment] = []
        if len(chain) >= 2:
            previous = None
            for position, article in enumerate(chain):
                when = status_time(article.status_id)
                segments.append(
                    ThreadSegment(
                        post_id=article.status_id,
                        index=position,
                        text=article.text,
                        timestamp=when.isoformat(),
                        gap_seconds=(
                            None
                            if previous is None
                            else int((when - previous).total_seconds())
                        ),
                        item_count=0,
                    )
                )
                previous = when
        stated, seen = reply_counts(articles, chain)
        return Manifest(
            source=ManifestSource(
                platform=PLATFORM,
                url=url,
                id=post_id,
                author=focal.handle,
                caption=focal.text,
                timestamp=status_time(focal.status_id).isoformat(),
                segments=segments,
                replies_stated=stated,
                replies_seen=seen,
            ),
            items=[],
        )


__all__ = ["PLATFORM", "READ_JS", "XAdapter"]
