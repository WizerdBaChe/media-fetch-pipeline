"""Read an X post and the author's own thread out of the rendered page.

Measured 2026-10-02 (docs/spike-09-x-page-read.md): in mfp's own Chrome,
logged out, the document answers 200 and the page renders the focal post, the
author's continuation posts and a few replies as `<article>` blocks. The page
makes no data request that carries the thread, so the rendered DOM is the only
source -- the weaker of the proposal's two candidates, chosen by measurement
rather than preference.

Three things about that DOM decide the shape of this module:

* **No stable test ids.** `data-testid` matched nothing. What is stable enough
  to anchor on is structural: `<article>`, the `/<handle>/status/<id>` links,
  the first `<div dir="auto">` (the post's words), and
  `data-engagement-action="reply"` (the reply count). Class names are
  Tailwind utilities and are never read.
* **Identity is the status id, never position** (P-88). A page renders other
  people's posts beside the one asked for, and the focal post appears more
  than once. The focal post is the article whose id equals the URL's.
* **Time is in the id.** The page shows 「9月30日」 and 「18 小時」, which are
  localised and relative. An X status id is a snowflake: its high bits are the
  creation time in milliseconds, so timestamps and gaps are computed rather
  than parsed from prose (P-77).

Nothing here judges meaning (D-88/D-149): it returns structure and the
author's words, which stay untrusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

#: Milliseconds from the Unix epoch to the snowflake epoch X counts from.
_SNOWFLAKE_EPOCH_MS = 1288834974657

_STATUS_HREF_RE = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)(?:[/?#]|$)")

#: An image under one of these is the post's own picture or a video's poster.
#: Profile photos, emoji and link-card previews are NOT media of the post.
_MEDIA_SRC_MARKERS = (
    "pbs.twimg.com/media/",
    "pbs.twimg.com/ext_tw_video_thumb/",
    "pbs.twimg.com/amplify_video_thumb/",
    "pbs.twimg.com/tweet_video_thumb/",
)

_COUNT_RE = re.compile(r"^\d[\d,]*$")


@dataclass(frozen=True)
class XArticle:
    """One `<article>` on the page, as the page rendered it."""

    status_id: str
    handle: str
    #: The post's words, or None when the article carries none.
    text: str | None
    #: A control inside the words was still present: the text is clipped.
    clipped: bool
    #: The reply count the page printed, when it is a plain integer. A
    #: localised abbreviation (「1.2萬」) is None, not a guess (P-72).
    replies: int | None
    #: Pictures or video posters that belong to the post.
    media: int

    @property
    def number(self) -> int:
        return int(self.status_id)


def status_time(status_id: str) -> datetime:
    """The creation time an X status id encodes."""
    return datetime.fromtimestamp(
        ((int(status_id) >> 22) + _SNOWFLAKE_EPOCH_MS) / 1000, tz=timezone.utc
    )


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.articles: list[_Raw] = []
        self._current: _Raw | None = None
        self._article_depth = 0
        self._body_div_depth = 0
        self._button_depth = 0
        self._counting = False
        self._in_reply_action = False

    # -- structure ------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        if tag == "article":
            if self._current is None:
                self._current = _Raw()
            self._article_depth += 1
            return
        current = self._current
        if current is None:
            return

        if tag == "a":
            match = _STATUS_HREF_RE.match(attributes.get("href", ""))
            if match:
                current.status_links.append((match.group(1), match.group(2)))
        elif tag == "div":
            if self._body_div_depth:
                self._body_div_depth += 1
            elif attributes.get("dir") == "auto" and not current.body_done:
                self._body_div_depth = 1
                current.body_started = True
        elif tag == "button" and self._body_div_depth:
            self._button_depth += 1
            current.clipped = True
        elif tag == "br" and self._body_div_depth:
            current.parts.append("\n")
        elif tag == "img":
            if self._body_div_depth and attributes.get("alt"):
                # An emoji is an <img> whose alt is the character.
                current.parts.append(attributes["alt"])
            source = attributes.get("src", "")
            if any(marker in source for marker in _MEDIA_SRC_MARKERS):
                current.media += 1
        elif tag == "video":
            current.media += 1

        action = attributes.get("data-engagement-action")
        if action is not None:
            self._in_reply_action = action == "reply"
        if (
            tag == "span"
            and self._in_reply_action
            and "data-animated-count-visual" in attributes
            and current.reply_text is None
        ):
            self._counting = True
            current.reply_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self._current is not None:
            self._article_depth -= 1
            if self._article_depth == 0:
                self.articles.append(self._current)
                self._current = None
                self._body_div_depth = 0
                self._button_depth = 0
                self._counting = False
                self._in_reply_action = False
            return
        if self._current is None:
            return
        if tag == "div" and self._body_div_depth:
            self._body_div_depth -= 1
            if self._body_div_depth == 0:
                self._current.body_done = True
        elif tag == "button" and self._button_depth:
            self._button_depth -= 1
        elif tag == "span" and self._counting:
            self._counting = False

    def handle_data(self, data: str) -> None:
        current = self._current
        if current is None:
            return
        if self._counting:
            current.reply_text = (current.reply_text or "") + data
        elif self._body_div_depth and not self._button_depth:
            current.parts.append(data)


@dataclass
class _Raw:
    status_links: list[tuple[str, str]]
    parts: list[str]
    clipped: bool
    media: int
    reply_text: str | None
    body_started: bool
    body_done: bool

    def __init__(self) -> None:
        self.status_links = []
        self.parts = []
        self.clipped = False
        self.media = 0
        self.reply_text = None
        self.body_started = False
        self.body_done = False


def _own_identity(links: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The `(handle, id)` the article is ABOUT.

    An article links to its own status several times (timestamp, view count,
    reply button) and to a quoted post at most once or twice, so the most
    frequent id is the article's own. A tie goes to the first link, which in
    every measured article was the post's own header.
    """
    if not links:
        return None
    counts: dict[str, int] = {}
    for _handle, status_id in links:
        counts[status_id] = counts.get(status_id, 0) + 1
    best = max(counts.values())
    for handle, status_id in links:
        if counts[status_id] == best:
            return handle, status_id
    return None


def _clean_text(parts: list[str]) -> str | None:
    text = "".join(parts).strip()
    return text or None


def read_articles(html: str) -> list[XArticle]:
    """Every distinct post on the page, in page order.

    The same post can be rendered twice (the focal post was, in the probe), so
    duplicates collapse on status id, keeping the copy that has words and, on
    a tie, the one that came first. An `<article>` that names no status of its
    own is not a post (an ad, a placeholder) and is dropped.
    """
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()

    ordered: list[XArticle] = []
    index: dict[str, int] = {}
    for raw in parser.articles:
        identity = _own_identity(raw.status_links)
        if identity is None:
            continue
        handle, status_id = identity
        reply_text = (raw.reply_text or "").strip()
        article = XArticle(
            status_id=status_id,
            handle=handle,
            text=_clean_text(raw.parts),
            clipped=raw.clipped,
            replies=int(reply_text.replace(",", "")) if _COUNT_RE.match(reply_text) else None,
            media=raw.media,
        )
        seen_at = index.get(status_id)
        if seen_at is None:
            index[status_id] = len(ordered)
            ordered.append(article)
        elif ordered[seen_at].text is None and article.text is not None:
            ordered[seen_at] = article
    return ordered


def locate_focal(articles: list[XArticle], post_id: str) -> XArticle | None:
    """The article whose status id is the URL's, or None -- never "the first"."""
    return next((a for a in articles if a.status_id == post_id), None)


def author_chain(articles: list[XArticle], focal: XArticle) -> list[XArticle]:
    """The focal post and the author's own posts that follow it directly.

    Contiguous in page order and the same account, with ids that keep rising.
    Contiguity is the point: the self-thread renders as one run, and the
    author's reply to some commenter further down is the same account but not
    a continuation. Ancestors above a focal reply are not collected.
    """
    start = articles.index(focal)
    chain = [focal]
    for article in articles[start + 1 :]:
        if article.handle.lower() != focal.handle.lower():
            break
        if article.number <= chain[-1].number:
            break
        chain.append(article)
    return chain


def reply_counts(
    articles: list[XArticle], chain: list[XArticle]
) -> tuple[int | None, int | None]:
    """`(stated, seen)`: what the page prints against what it shipped.

    Never derived from each other (the Threads rule). `seen` counts posts on
    the page that are not part of the chain, which for a logged-out page is a
    handful; `stated` is the focal post's printed reply count.
    """
    stated = chain[0].replies
    in_chain = {article.status_id for article in chain}
    seen = sum(1 for article in articles if article.status_id not in in_chain)
    return stated, (seen if (stated is not None or seen) else None)


__all__ = [
    "XArticle",
    "author_chain",
    "locate_focal",
    "read_articles",
    "reply_counts",
    "status_time",
]
