"""Fixture capture for M2 (`mfp capture`).

M2's acceptance is "given the two spike URLs, saves `outerHTML` to fixtures".
This is that command, and it is the one part of M2/M3 that cannot be done
without the network: `extract.py` is written against the field names spike-01
and spike-02 recorded, but nothing proves it matches a live page until a live
page has been run through it. That is the gap this closes.

Deliberately a separate command rather than something `probe` does. Capturing
is a development action with a different contract: it writes into the repo's
`tests/fixtures/`, it is expected to be run rarely and read often, and its
output is committed. Folding it into `probe` would make every ordinary run
carry an "am I also writing fixtures?" branch.

Each capture also writes a `.meta.json` beside the HTML recording what was
asked for and what came back, because a fixture with no provenance is a file
nobody dares delete and nobody dares trust.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from mfp.adapters.instagram.chrome import Connector, Launcher, chrome_session
from mfp.config import AppConfig
from mfp.adapters.instagram.extract import extract_items, looks_like_login_wall
from mfp.redact import redact_and_verify

#: Where captured pages live. Committed, so `extract.py` has something to be
#: right or wrong about without anyone touching the network.
DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

_SHORTCODE_RE = re.compile(r"/(?:p|reel|reels|tv|share|post)/([A-Za-z0-9_-]+)")


def fixture_name(url: str) -> str:
    """A stable, filesystem-safe name for a captured page.

    The digest is load-bearing: two platforms can use the same shortcode
    alphabet, and a collision would silently overwrite the other's fixture.
    """
    match = _SHORTCODE_RE.search(url)
    shortcode = match.group(1) if match else "page"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{shortcode}-{digest}"


@dataclass
class CaptureResult:
    url: str
    name: str
    html_path: Path | None
    meta_path: Path | None
    html_length: int = 0
    item_count: int = 0
    variant_count: int = 0
    error: str | None = None
    #: False when the scrubbed copy extracted differently and was refused.
    redaction_ok: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None


def _default_launcher(argv: list[str]):
    # argv-array invocation only -- never a shell string (Phase 2 §4.2).
    # stdin=DEVNULL: Chrome outlives the call, and a long-lived child holding
    # a duplicate of our stdin has no business doing so. See
    # `doctor._default_runner` for the measured failure in this class.
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, shell=False)


def capture_urls(
    urls: Sequence[str],
    config: AppConfig,
    *,
    fixture_root: Path = DEFAULT_FIXTURE_ROOT,
    platform: str = "instagram",
    state_dir: Path,
    connector: Connector,
    http_get: Callable[[str], str],
    launcher: Launcher | None = None,
    on_progress: Callable[[str], None] = lambda _m: None,
) -> list[CaptureResult]:
    """Capture each URL's `outerHTML` into `fixture_root/<platform>/`.

    One browser for the whole batch: launching per URL would multiply the
    4-6s startup measured in spike-02 §1 by the number of posts, for nothing.

    A failure on one URL does not abandon the rest -- a nine-URL capture that
    dies on the third and reports nothing is worse than eight fixtures and a
    named failure.
    """
    target_dir = fixture_root / platform
    target_dir.mkdir(parents=True, exist_ok=True)
    results: list[CaptureResult] = []

    with chrome_session(
        config.chrome,
        state_dir=state_dir,
        launcher=launcher or _default_launcher,
        connector=connector,
        http_get=http_get,
    ) as session:
        for index, url in enumerate(urls, start=1):
            name = fixture_name(url)
            on_progress(f"[{index}/{len(urls)}] {url}")
            try:
                html = session.fetch_html(url)
            except Exception as exc:  # noqa: BLE001 -- one bad URL must not end the batch
                results.append(CaptureResult(url=url, name=name, html_path=None,
                                             meta_path=None, error=f"{type(exc).__name__}: {exc}"))
                continue

            # The raw page is somebody's post. It stays out of git (see
            # tests/fixtures/_raw/.gitignore) and exists so a failure can be
            # investigated against what actually arrived.
            raw_dir = fixture_root / "_raw" / platform
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"{name}.html").write_text(html, encoding="utf-8")

            redacted, redaction = redact_and_verify(html)
            html_path = target_dir / f"{name}.html"
            if redaction.preserved:
                html_path.write_text(redacted, encoding="utf-8")
            else:
                # Refusing is the point. A redacted fixture that extracts
                # differently is testing a shape the redaction invented.
                html_path = None  # type: ignore[assignment]

            # Run the extractor immediately. The whole point of the fixture is
            # to answer "does extract.py work on a real page?", and answering
            # it now costs nothing and turns a silent zero into a visible one.
            items = extract_items(html)
            variant_count = sum(len(item.variants) for item in items)

            meta = {
                "url": url,
                "platform": platform,
                "htmlLength": len(html),
                "itemCount": len(items),
                "variantCount": variant_count,
                "kinds": [item.kind for item in items],
                # The same predicate the adapter uses, so a fixture cannot
                # report a state the adapter would disagree with.
                "hasLoginForm": looks_like_login_wall(html),
                # Provenance for the committed copy: how much was scrubbed,
                # and whether the scrub was proved harmless.
                "redaction": {
                    "committed": redaction.preserved,
                    "urlsRewritten": redaction.urls_rewritten,
                    "valuesReplaced": redaction.values_replaced,
                    # The leak half of the check, recorded because it is the
                    # half that decides whether this file may be in git at
                    # all. It was missing here while the two committed
                    # fixtures carried it from a one-off audit script, so a
                    # re-capture would have silently dropped the evidence and
                    # nothing would have failed (2026-08-16).
                    "secretsLeaked": redaction.secrets_leaked,
                    "itemsBeforeAfter": [redaction.items_before, redaction.items_after],
                    "variantsBeforeAfter": [
                        redaction.variants_before,
                        redaction.variants_after,
                    ],
                },
            }
            meta_path = target_dir / f"{name}.meta.json"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            results.append(
                CaptureResult(
                    url=url,
                    name=name,
                    html_path=html_path,
                    meta_path=meta_path,
                    html_length=len(html),
                    item_count=len(items),
                    variant_count=variant_count,
                    redaction_ok=redaction.preserved,
                )
            )
    return results


def summarize(results: Sequence[CaptureResult]) -> str:
    """A report that names the zero-item captures rather than averaging them
    away -- a fixture that extracted nothing is the single most important
    line in this output."""
    lines: list[str] = []
    for result in results:
        if not result.ok:
            lines.append(f"  FAIL  {result.name}: {result.error}")
        elif result.item_count > 0 and not result.redaction_ok:
            lines.append(
                f"  RAW   {result.name}: {result.item_count} item(s) captured, but the "
                "redacted copy extracted differently and was NOT written. The raw page "
                "is under tests/fixtures/_raw/ and is gitignored."
            )
        elif result.item_count == 0:
            lines.append(
                f"  EMPTY {result.name}: {result.html_length} bytes, 0 items "
                "<- extract.py found nothing; this is the case to investigate"
            )
        else:
            lines.append(
                f"  OK    {result.name}: {result.item_count} item(s), "
                f"{result.variant_count} variant(s)"
            )

    captured = sum(1 for r in results if r.ok)
    empty = sum(1 for r in results if r.ok and r.item_count == 0)
    lines.append("")
    lines.append(f"captured {captured}/{len(results)}; {empty} yielded no media")
    return "\n".join(lines)


__all__ = ["CaptureResult", "DEFAULT_FIXTURE_ROOT", "capture_urls", "fixture_name", "summarize"]
