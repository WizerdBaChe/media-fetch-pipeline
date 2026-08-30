"""Media extraction from an Instagram/Threads page (PSM §5.4, normative).

Read the trap list before changing anything here. Three of the four traps
produce *silently wrong output* rather than an error, which is why this
module drops data it cannot vouch for instead of passing it along:

- **TRAP-1** — script JSON escapes `/` as `\\/`. Unescape before any regex or
  you get zero matches and conclude, wrongly, that the page changed.
- **TRAP-2** — never infer media type from the extension. Images are served
  as `....heic?stp=dst-jpg_e35_tt6`: a `.heic` URL delivering JPEG bytes.
- **TRAP-3** — `static.cdninstagram.com` hosts the site's own UI assets, not
  post media.
- **TRAP-4** — the same asset appears twice on the page: clean inside the
  JSON, and HTML-entity-encoded in the DOM attributes. A regex over raw HTML
  finds the second one, producing `amp;oh=` instead of `oh=`. That URL looks
  perfectly valid and fails only at download time with 403 `Bad URL hash`.
  Measured (spike-02 §3.2): one Reel yielded 6 regex-found video URLs of
  which 1 was downloadable; one carousel yielded 149 image URLs, most broken.

Hence the two hard rules: media URLs come **only** from parsed JSON fields,
and a URL without both `oh=` and `oe=` never enters `variants`. Dropping such
a URL is correct — writing it to the manifest hands the user a 0-byte file
hours later, with nothing on screen to explain it.

Node location is by **key presence**, not by path. The payload's nesting has
changed before and will again; the field names (`carousel_media`,
`video_versions`, `image_versions2`) are the stable part.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Literal
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from mfp.errors import UpstreamStructureChange
from mfp.models import ExcludedFormats, MediaItem, Variant

#: Hosts whose assets are never post media (TRAP-3).
UI_ASSET_HOSTS: frozenset[str] = frozenset({"static.cdninstagram.com"})

#: Both must be present for a CDN URL to be downloadable (spike-02 §3.2).
REQUIRED_SIGNATURE_PARAMS: tuple[str, ...] = ("oh", "oe")

_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"[{\[]")

#: `media_type` as Instagram reports it. 8 is a carousel container, which is
#: not itself an item -- it is the reason `carousel_media` is checked first.
_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2


def unescape_script_json(text: str) -> str:
    r"""Undo the `\/` escaping used throughout the embedded JSON (TRAP-1).

    Applied before anything else looks at the text. Omitting it does not
    raise -- it just finds nothing, which reads exactly like "the page
    structure changed" and sends the reader off to rewrite the extractor.
    """
    return text.replace("\\/", "/")


def iter_script_payloads(html: str) -> Iterator[Any]:
    """Yield every JSON value embedded in a `<script>` tag.

    Non-JSON scripts are skipped silently: a page carries plenty of ordinary
    JavaScript and none of it is a defect.
    """
    for body in _SCRIPT_RE.findall(html):
        text = unescape_script_json(body).strip()
        if not text:
            continue
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            continue
        try:
            yield json.loads(text[match.start():])
        except json.JSONDecodeError:
            # A script that merely CONTAINS JSON among other statements
            # (`requireLazy([...], function(){ return {...} })`).
            for candidate in balanced_object_slices(text):
                try:
                    yield json.loads(candidate)
                except json.JSONDecodeError:
                    continue


#: How many `{` positions to try per script before giving up. The payload
#: object is near the front of the scripts that carry one; scanning every
#: brace of a multi-megabyte page would be quadratic for no gain.
MAX_OBJECT_CANDIDATES = 24


def balanced_object_slices(text: str, *, limit: int = MAX_OBJECT_CANDIDATES) -> Iterator[str]:
    """Yield brace-balanced `{...}` substrings, one per opening brace.

    String-aware: a `{` or `}` inside a JSON string literal must not move the
    depth counter, or every payload containing `"a}b"` slices in the wrong
    place and silently fails to parse -- which then looks like a structure
    change rather than a bug here.
    """
    tried = 0
    for match in re.finditer(r"\{", text):
        if tried >= limit:
            return
        tried += 1
        end = _matching_brace(text, match.start())
        if end is not None:
            yield text[match.start() : end + 1]


def _matching_brace(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def walk_objects(payload: Any) -> Iterator[dict[str, Any]]:
    """Depth-first walk over every dict in a decoded payload."""
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from walk_objects(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from walk_objects(value)


def is_signed_media_url(url: str) -> bool:
    """The TRAP-4 / spike-02 §3.2 validity predicate.

    A downloadable CDN URL always carries both `oh=` and `oe=`. An
    entity-mangled copy carries `amp;oh=` and `amp;oe=` instead, which this
    correctly rejects — that is the entire point.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    split = urlsplit(url)
    if split.hostname in UI_ASSET_HOSTS:  # TRAP-3
        return False
    params = parse_qs(split.query)
    return all(key in params for key in REQUIRED_SIGNATURE_PARAMS)


def expires_at_from_url(url: str) -> str | None:
    """Decode `oe=` (hex unix seconds) into an ISO-8601 UTC string (§5.7).

    Measured TTL ≈ 32h on the sampled video URL. Recorded per variant so
    `fetch` can refuse an expired link by name instead of handing the user a
    folder of 403s. Returns None when it cannot be decoded — an undecodable
    value must not become a bogus expiry that expires everything.
    """
    try:
        raw = parse_qs(urlsplit(url).query)["oe"][0]
        seconds = int(raw, 16)
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (KeyError, IndexError, ValueError, OSError, OverflowError):
        return None


def quality_tag(url: str) -> str | None:
    """Decode the `efg` query parameter to its `venncode_tag` (§5.4 step 6).

    Observed: `...CLIPS.C3.720.dash_baseline_1_v1` (video, 720) and
    `CAROUSEL_ITEM.xpids.1440.sdr.regular_photo.C3` (image, 1440). Best
    effort by design: it is a labelling aid, and a missing label must never
    stop a downloadable variant from being offered.
    """
    try:
        raw = parse_qs(urlsplit(url).query)["efg"][0]
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(padded).decode("utf-8"))
    except (KeyError, IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    tag = decoded.get("vencode_tag") or decoded.get("venncode_tag")
    return tag if isinstance(tag, str) else None


def height_from_tag(tag: str | None) -> int | None:
    """Pull the resolution out of a venncode tag (`...C3.720.dash...`)."""
    if not tag:
        return None
    for part in tag.split("."):
        if part.isdigit() and 100 <= int(part) <= 5000:
            return int(part)
    return None


#: The resize token inside `stp`: `_p720x720_` or `_s1080x1080_`. The LETTER
#: is the whole meaning -- see `StpTransform.kind`. `sh2.08` (sharpening) sits
#: in the same list and starts with `s`; requiring digits either side of the
#: `x` is what keeps it out.
_STP_RESIZE_RE = re.compile(r"(?:^|_)([sp])(\d+)x(\d+)(?:_|$)")

#: The same shape without the underscore boundaries. Its job is to catch a
#: token grammar that has MOVED: if this matches where the strict pattern did
#: not, the URL is describing a resize this code cannot read, and the honest
#: answer is "unknown size" rather than "no resize, so it is the original".
_STP_RESIZE_LOOSE_RE = re.compile(r"[sp]\d+x\d+")

#: The crop box: `c0.140.1122.1122a` -- x0, y0, width, height.
_STP_CROP_RE = re.compile(r"(?:^|_)c(\d+)\.(\d+)\.(\d+)\.(\d+)a(?:_|$)")


@dataclass(frozen=True)
class StpTransform:
    """What the CDN did to the original, read from the URL's `stp` parameter.

    Measured 2026-08-25 on one carousel (7 items, 91 variants, all the same
    shape) and cross-checked row-for-row against downloading all thirteen
    renditions of item 0 -- the table is in `docs/spike-05-image-ladder.md`.

    **`kind` comes from the letter, never from the box's proportions.**
    `p720x720` is a SQUARE box that yields a 720x900 image: `p` fits the box
    by the short edge and lets the long edge overflow. Classifying by
    `width == height` would mark every aspect-preserving rung as a crop, and
    the exclusion below would then throw away the entire usable ladder. That
    mistake passes every other test in the file, so there is one test whose
    only job is to catch it.

    The reason this was found late is worth keeping: `quality_tag()` decodes
    the `efg` parameter and nothing had ever looked at `stp`, so its `None`
    was read as "this URL declares no size" (P-40).
    """

    #: `square` -- an `sNxN` crop, not a rendition of a non-square item.
    #: `aspect` -- a `pNxN` scale, the same picture smaller.
    #: `original` -- no resize token at all.
    #: `unknown` -- something resize-SHAPED is in there that this grammar
    #: cannot read. Kept separate from `original` deliberately: collapsing the
    #: two would report a shrunken rendition at the original's full size, and
    #: a confident wrong number is the exact failure this module was just
    #: repaired for (P-38, P-40). An unreadable token must degrade to an
    #: unknown size, which `brief`'s ranged GET then measures.
    kind: Literal["square", "aspect", "original", "unknown"]
    #: The `NxN` the token asked for. None for `original`.
    box: tuple[int, int] | None
    #: `(x0, y0, width, height)` when a crop box is present.
    crop: tuple[int, int, int, int] | None


def parse_stp(url: str) -> StpTransform | None:
    """Decode the `stp` query parameter, or None when there is none.

    Pure and free: no network, no guess. A URL with no `stp` is not an error
    -- Threads and Reel covers carry real `width`/`height` fields instead.
    """
    try:
        raw = parse_qs(urlsplit(url).query)["stp"][0]
    except (KeyError, IndexError, ValueError):
        return None

    crop_match = _STP_CROP_RE.search(raw)
    crop = (
        (
            int(crop_match.group(1)),
            int(crop_match.group(2)),
            int(crop_match.group(3)),
            int(crop_match.group(4)),
        )
        if crop_match
        else None
    )

    resize = _STP_RESIZE_RE.search(raw)
    if resize is None:
        unreadable = _STP_RESIZE_LOOSE_RE.search(raw) is not None
        return StpTransform(kind="unknown" if unreadable else "original", box=None, crop=crop)
    kind = "square" if resize.group(1) == "s" else "aspect"
    box = (int(resize.group(2)), int(resize.group(3)))
    return StpTransform(kind=kind, box=box, crop=crop)


def original_size_from_crop(crop: tuple[int, int, int, int] | None) -> tuple[int, int] | None:
    """`(width, height)` of the image a centred crop box was taken from.

    `c0.140.1122.1122a` means "take 1122x1122 starting at y=140", so the
    original was 1122 x (1122 + 140*2) = 1122x1402 -- which is exactly what
    downloading it reported.

    Returns None when `x0 != 0`: a crop that is not full-width says nothing
    about the original's width, and an inference that does not hold must
    return None rather than a number.
    """
    if crop is None:
        return None
    x0, y0, width, height = crop
    if x0 != 0:
        return None
    return width, height + 2 * y0


def _scaled_to_short_edge(original: tuple[int, int], short_edge: int) -> tuple[int, int]:
    """Scale `original` so its SHORT edge becomes `short_edge`.

    That is what a square `pNxN` box does, and the short edge is also what
    `policy.resolution_class` compares (D-39), so the two line up without a
    conversion. Verified against the download table: 1122x1402 with N=720
    gives 720x900, N=640 gives 640x800, and so on for 480/320/240.
    """
    width, height = original
    if width <= height:
        return short_edge, round(short_edge * height / width)
    return round(short_edge * width / height), short_edge


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ext_for(kind: str, mime: str | None = None) -> str:
    """Extension by declared type, never by the URL (TRAP-2)."""
    if mime:
        if "mp4" in mime:
            return "mp4"
        if "webm" in mime:
            return "webm"
    if kind == "video":
        return "mp4"
    # An audio track with no usable mime would otherwise fall through to the
    # image default and be written as `.jpg` -- TRAP-2's own failure mode,
    # committed by the function that exists to prevent it.
    if kind == "audio":
        return "m4a"
    return "jpg"


@dataclass(frozen=True)
class ImageCandidates:
    """What one item's `candidates[]` yielded, and what was held back.

    Two lists rather than one, because `Manifest.excluded[]` is how this
    project makes a filtering decision visible instead of silent -- the same
    reason `ytdlp` counts storyboards.
    """

    renditions: list[Variant]
    square_crops_dropped: int


def classify_image_candidates(node: dict[str, Any]) -> ImageCandidates:
    """`image_versions2.candidates[]` -> renditions, with crops separated.

    Sizes come from the CANDIDATE, never from the item (INV-B1). In order:
    the candidate's own `width`/`height` fields, then its own `stp` transform
    tokens, then `None`.

    The item-level venncode tag is deliberately absent from that list. It is
    one string shared by all thirteen candidates and it describes the
    ORIGINAL, so using it as a per-variant height stamped one fabricated rung
    (`1122`, which is the original's WIDTH) across the whole menu: the sort
    key tied, `policy.choose` fell to list position zero, and `--policy
    smallest` returned the same file as `best` on every Instagram photo post
    this tool has ever fetched. `progressive_video_variants` keeps the tag
    fallback, where it is per-representation and the fields are usually there.
    """
    candidates = (node.get("image_versions2") or {}).get("candidates") or []

    rows: list[tuple[str, dict[str, Any], StpTransform | None]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url")
        if not is_signed_media_url(url):
            continue
        rows.append((url, candidate, parse_stp(url)))

    original = _original_size(rows)

    renditions: list[Variant] = []
    crops: list[Variant] = []
    for url, candidate, stp in rows:
        size = _declared_size(candidate) or _size_from_stp(stp, original)
        variant = Variant(
            url=url,
            width=size[0] if size else None,
            height=size[1] if size else None,
            ext=_ext_for("image"),
            has_audio=False,
            expires_at=expires_at_from_url(url),
        )
        (crops if _is_square_crop(stp, size) else renditions).append(variant)

    # Only drop the crops when something survives them. An item that is
    # genuinely square offers nothing else, and a square original's square
    # rendition is the whole picture, not a crop of it.
    if not renditions:
        return ImageCandidates(renditions=crops, square_crops_dropped=0)
    return ImageCandidates(renditions=renditions, square_crops_dropped=len(crops))


def _original_size(
    rows: list[tuple[str, dict[str, Any], StpTransform | None]],
) -> tuple[int, int] | None:
    """The size of the picture every `pNxN` row is a scale of, or None.

    The aspect rungs need it: `p720x720` fixes the SHORT edge at 720 and the
    ratio supplies the rest. Getting it wrong is worse than not having it,
    because a wrong ratio produces a confident wrong number on every rung of
    the item -- and `brief`'s ranged-GET fallback only repairs sizes that are
    `None`, so a wrong one has no safety net behind it. Hence two sources and
    no third:

    1. **A row that SAYS it is the original and declares its size.** A direct
       claim, and the strongest thing available.
    2. **Agreeing crop boxes.** `c0.140.1122.1122a` implies 1122x1402 for a
       centred crop, verified against the download for the measured item. If
       two siblings imply DIFFERENT originals they cannot both be right, and
       an item whose own rows disagree is one to report unknown rather than
       to pick a winner from.

    A declared size on a row that merely lacks `stp` is deliberately NOT a
    source. "No transform recorded" is not "this is the full picture": in a
    payload mixing declared rows with `stp`-only ones, the first declared row
    could be a 150x150 thumbnail, and scaling every aspect rung off that
    ratio would poison the whole item.
    """
    for _url, candidate, stp in rows:
        if stp is not None and stp.kind == "original" and (size := _declared_size(candidate)):
            return size

    implied = {
        recovered
        for _url, _candidate, stp in rows
        if stp is not None and (recovered := original_size_from_crop(stp.crop))
    }
    return implied.pop() if len(implied) == 1 else None


def _declared_size(candidate: dict[str, Any]) -> tuple[int, int] | None:
    """The candidate's own `width`/`height`, when it carries both."""
    width = _int_or_none(candidate.get("width"))
    height = _int_or_none(candidate.get("height"))
    return (width, height) if width and height else None


def _size_from_stp(
    stp: StpTransform | None, original: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Resolve a size from the transform tokens, or None -- never a guess."""
    if stp is None:
        return None
    if stp.kind == "square":
        return stp.box
    if stp.kind == "original":
        return original
    if stp.kind == "unknown":
        # Something resized this and the grammar moved. Reporting `original`
        # here would hand back a full-size number for a shrunken picture.
        return None
    # `aspect`: the short-edge identity is only proven for a SQUARE box, and
    # the ratio has to come from somewhere. Either missing -> unknown, which
    # is what `brief`'s ranged-GET fallback exists to fill in.
    if stp.box is None or stp.box[0] != stp.box[1] or original is None:
        return None
    return _scaled_to_short_edge(original, stp.box[0])


def _is_square_crop(stp: StpTransform | None, size: tuple[int, int] | None) -> bool:
    """Is this row a crop rather than a rendition (D-92)?

    The token decides when there is one. Threads and Reel covers carry no
    `stp` but do declare real dimensions, and they serve both ladders too --
    so with no token, a square row is judged a crop by its geometry. An
    `original` row is never a crop even when it happens to be square.
    """
    if stp is not None:
        return stp.kind == "square"
    return size is not None and size[0] == size[1]


def image_variants(node: dict[str, Any]) -> list[Variant]:
    """`image_versions2.candidates[]` -> Variants, unsigned ones dropped."""
    return classify_image_candidates(node).renditions


def progressive_video_variants(node: dict[str, Any]) -> list[Variant]:
    """`video_versions[]` -> Variants. Muxed already, so no ffmpeg needed."""
    variants: list[Variant] = []
    for version in node.get("video_versions") or []:
        if not isinstance(version, dict):
            continue
        url = version.get("url")
        if not is_signed_media_url(url):
            continue
        tag = quality_tag(url)
        variants.append(
            Variant(
                url=url,
                width=_int_or_none(version.get("width")),
                height=_int_or_none(version.get("height")) or height_from_tag(tag),
                ext=_ext_for("video"),
                has_audio=True,
                needs_mux=False,
                expires_at=expires_at_from_url(url),
            )
        )
    return variants


#: Elements that mean "this representation is a sequence of segments", not
#: one downloadable file. `SegmentBase` is deliberately absent: it describes a
#: byte-range index *into a single file*, which the transfer layer can fetch.
SEGMENTED_ADDRESSING_TAGS: frozenset[str] = frozenset({"SegmentTemplate", "SegmentList"})


@dataclass(frozen=True)
class DashRepresentation:
    """One `<Representation>`, with the addressing mode resolved.

    `addressing` is the field that matters. A `segmented` representation
    cannot be fetched with a ranged GET, so offering it as a variant would
    put a "1080p" option in front of the user that the download layer cannot
    honour — the same class of silent lie as TRAP-4.
    """

    url: str | None
    mime: str
    addressing: str  # "single_file" | "segmented" | "unknown"
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None

    @property
    def is_video(self) -> bool:
        return self.mime.startswith("video/")


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


#: A bare `&` that is not already the start of an entity reference.
_BARE_AMPERSAND_RE = re.compile(r"&(?!#?\w+;)")


def _parse_xml(manifest_xml: str) -> ElementTree.Element | None:
    """Parse an MPD, repairing unescaped `&` before giving up.

    XML requires `&` to be written `&amp;`, and a well-formed manifest does
    exactly that — which is *also* why an XML parser is the right tool here
    rather than a regex. A regex over the raw text reads the escaped form
    literally, producing `amp;oh=` and hitting the TRAP-4 predicate, so every
    DASH URL is silently dropped. The parser decodes it back to `&` and the
    URLs are usable.

    The repair covers the other direction: a manifest that arrives with bare
    ampersands is not well-formed, and refusing it would turn a cosmetic
    upstream slip into "this post has no 1080p".
    """
    try:
        return ElementTree.fromstring(manifest_xml)
    except ElementTree.ParseError:
        pass
    try:
        return ElementTree.fromstring(_BARE_AMPERSAND_RE.sub("&amp;", manifest_xml))
    except ElementTree.ParseError:
        # Genuinely unreadable. The progressive versions still stand; half
        # reading it with a regex is what this replaced.
        return None


def parse_dash_manifest(manifest_xml: str | None) -> list[DashRepresentation]:
    """Parse an MPD into representations, inheriting AdaptationSet attributes.

    Uses a real XML parser rather than a regex, and both reasons were
    measured against a live third-party manifest (`dash.akamaized.net`,
    2026-08-16) on which the previous regex version found **zero**
    representations:

    - 10 of its 11 `<Representation>` elements are **self-closing**, so a
      `<Representation>…</Representation>` pattern never matches them.
    - `mimeType` sits on the enclosing `<AdaptationSet>`, not on the
      representation, so a filter reading it from the representation's own
      attributes rejects everything.

    Neither would ever have surfaced against a hand-built fixture, because a
    hand-built fixture encodes the same assumptions as the parser.
    """
    if not manifest_xml or not manifest_xml.strip():
        return []
    root = _parse_xml(manifest_xml)
    if root is None:
        return []

    # Representations are found wherever they sit, and attributes are
    # inherited from whatever ancestors happen to exist. Requiring the full
    # MPD/Period/AdaptationSet chain would reject a manifest fragment, and
    # Instagram embeds its manifest as a JSON string of unknown completeness.
    parents = {child: parent for parent in root.iter() for child in parent}

    def ancestors(element: ElementTree.Element) -> Iterator[ElementTree.Element]:
        current = parents.get(element)
        while current is not None:
            yield current
            current = parents.get(current)

    found: list[DashRepresentation] = []
    for element in root.iter():
        if _strip_namespace(element.tag) != "Representation":
            continue
        chain = list(ancestors(element))
        inherited_mime = next(
            (parent.get("mimeType") for parent in chain if parent.get("mimeType")), ""
        )
        inherited_segmented = any(_has_segmented_addressing(parent) for parent in chain)
        found.append(
            _representation(
                element,
                inherited_mime=inherited_mime or "",
                inherited_segmented=inherited_segmented,
            )
        )
    return found


def _has_segmented_addressing(element: ElementTree.Element) -> bool:
    """True when this element declares segment-based addressing directly."""
    return any(_strip_namespace(child.tag) in SEGMENTED_ADDRESSING_TAGS for child in element)


def _representation(
    element: ElementTree.Element, *, inherited_mime: str, inherited_segmented: bool
) -> DashRepresentation:
    mime = element.get("mimeType") or inherited_mime
    base_url = None
    for child in element:
        if _strip_namespace(child.tag) == "BaseURL" and child.text:
            base_url = unescape_script_json(child.text.strip())
            break

    if inherited_segmented or _has_segmented_addressing(element):
        addressing = "segmented"
    elif base_url:
        addressing = "single_file"
    else:
        addressing = "unknown"

    return DashRepresentation(
        url=base_url,
        mime=mime,
        addressing=addressing,
        width=_int_or_none(element.get("width")),
        height=_int_or_none(element.get("height")),
        bitrate=_int_or_none(element.get("bandwidth")),
    )


def dash_variants(manifest_xml: str | None) -> list[Variant]:
    """The downloadable video representations of a DASH manifest.

    This is not an optimisation. Measured (spike-02 §2): the progressive
    `video_versions` of a Reel topped out at 720p while its DASH manifest
    carried 1080p. A `best` policy that reads only `video_versions` therefore
    hands over 720p while claiming it is the best available — silently wrong,
    which is the failure mode this whole module is organised against.

    The tracks are separate (video-only + audio-only), so anything sourced
    here carries `needs_mux`, and that is what makes ffmpeg a hard dependency
    of `best` rather than an optional extra (§14.1).

    Segmented representations are **excluded**, not downgraded: see
    `dash_unavailable` for what the adapter should say about them.
    """
    variants: list[Variant] = []
    for representation in parse_dash_manifest(manifest_xml):
        if not representation.is_video or representation.addressing != "single_file":
            continue
        url = representation.url
        if not is_signed_media_url(url):
            continue
        variants.append(
            Variant(
                url=url,
                width=representation.width,
                height=representation.height,
                bitrate=representation.bitrate,
                ext=_ext_for("video", representation.mime),
                has_audio=False,
                needs_mux=True,
                expires_at=expires_at_from_url(url),
            )
        )
    return variants


def dash_audio_variant(manifest_xml: str | None) -> Variant | None:
    """The audio track that `dash_variants`' video-only renditions need.

    Every variant `dash_variants` returns carries `needs_mux`, which is a
    promise that a second stream exists. Until 2026-08-17 this function did
    not, so that promise pointed at nothing: the audio `<Representation>` was
    parsed and then dropped on the floor, and the download layer had no way
    to find the sound for a 1080p Reel.

    Highest bitrate wins. Instagram ships one audio adaptation per post in
    every manifest measured so far, so this is a tie-break that has not yet
    had to break a tie -- written as a rule anyway, because silently taking
    document order would degrade the audio the day a second one appears.
    """
    candidates = [
        representation
        for representation in parse_dash_manifest(manifest_xml)
        if representation.mime.startswith("audio/")
        and representation.addressing == "single_file"
        and is_signed_media_url(representation.url)
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda representation: representation.bitrate or 0)
    url = best.url
    assert url is not None  # is_signed_media_url() rejects None
    return Variant(
        url=url,
        bitrate=best.bitrate,
        ext=_ext_for("audio", best.mime),
        has_audio=True,
        needs_mux=False,
        expires_at=expires_at_from_url(url),
    )


def dash_unavailable(manifest_xml: str | None) -> list[DashRepresentation]:
    """Video representations this build cannot fetch, highest first.

    Exists so the omission can be *said* rather than silently applied. If a
    manifest offers 1080p only as a segment sequence, the honest report is
    "1080p exists but needs segment assembly, which this build does not do" —
    not a 720p download presented as the best available.
    """
    return sorted(
        (
            representation
            for representation in parse_dash_manifest(manifest_xml)
            if representation.is_video and representation.addressing == "segmented"
        ),
        key=lambda representation: representation.height or 0,
        reverse=True,
    )


def _kind_of(node: dict[str, Any]) -> str:
    """Declared type only, never the file extension (TRAP-2)."""
    media_type = _int_or_none(node.get("media_type"))
    if media_type == _MEDIA_TYPE_VIDEO:
        return "video"
    if media_type == _MEDIA_TYPE_IMAGE:
        return "image"
    return "video" if node.get("video_versions") else "image"


def _build_item(node: dict[str, Any], index: int) -> tuple[MediaItem | None, int]:
    """`build_item`, plus how many square crops it held back.

    The count has to travel: `Manifest.excluded[]` is what keeps a filtering
    decision from being a silent one, and the decision is made down here.
    """
    kind = _kind_of(node)
    audio: Variant | None = None
    crops = 0
    if kind == "video":
        dash_manifest = node.get("video_dash_manifest")
        variants = progressive_video_variants(node) + dash_variants(dash_manifest)
        audio = dash_audio_variant(dash_manifest)
    else:
        classified = classify_image_candidates(node)
        variants = classified.renditions
        crops = classified.square_crops_dropped

    if not variants:
        return None, crops

    alt_text = node.get("accessibility_caption")
    return (
        MediaItem(
            index=index,
            kind=kind,
            variants=variants,
            audio=audio,
            alt_text=alt_text if isinstance(alt_text, str) else None,
        ),
        crops,
    )


def build_item(node: dict[str, Any], index: int) -> MediaItem | None:
    """One media node -> one MediaItem, or None when nothing survived.

    None rather than an empty item: an item with no variants is a row the
    user can select and cannot download.
    """
    return _build_item(node, index)[0]


@dataclass(frozen=True)
class PostMetadata:
    """Who posted it, what they said, and when -- all optional.

    Not cosmetic. The output layout is
    `<platform>/<author>/<date>_<postId>/` (§8), so without these every post
    ever downloaded lands in `unknown_author/` under the date it was
    *fetched* rather than posted -- an author directory that never varies
    and a date that describes the downloader instead of the post. Measured
    on the first real fetch, 2026-08-17.
    """

    #: The post's own shortcode, which is not always the one in the URL: a
    #: Threads share link (`/share/BAVhpgxAWW/`) resolves to a post whose
    #: `code` is `Dbo3FLAk5Zi`. The glossary calls `share:<code>` a
    #: *provisional* id "rewritten at probe time"; this is what rewrites it.
    #: Without it the same post reached two ways produces two directories.
    code: str | None = None
    author: str | None = None
    caption: str | None = None
    timestamp: str | None = None


def find_post_node(payload: Any) -> dict[str, Any] | None:
    """The node the media came from -- `find_media_nodes`' own anchor.

    Two rejected alternatives, both measured on the real fixtures
    (2026-08-17):

    * *Match the shortcode.* A captured Instagram page carries 20 distinct
      `code` values, and the first node bearing the one we asked for is a
      lightweight reference with no `taken_at` and no `user`. Matching it
      returned nothing on both Instagram pages.
    * *Take the first node carrying `taken_at`.* The carousel page has 8 of
      them and the Threads page 37 -- related posts, recommendations. That
      does not fail, it silently files the download under a stranger's name
      and date, which is worse.

    Anchoring to the media node makes the metadata and the media
    structurally incapable of describing different posts. It is also what
    makes Threads work: its share code (`BAVhpgxAWW`) is not the `code` on
    the node it resolves to (`Dbo3FLAk5Zi`).
    """
    for node in walk_objects(payload):
        carousel = node.get("carousel_media")
        if isinstance(carousel, list) and carousel:
            return node
    for key in ("video_versions", "image_versions2"):
        for node in walk_objects(payload):
            if node.get(key):
                return node
    return None


def post_metadata(html: str) -> PostMetadata:
    """Pull the post-level fields off the node the media came from."""
    node: dict[str, Any] | None = None
    for payload in iter_script_payloads(html):
        node = find_post_node(payload)
        if node is not None:
            break

    if node is None:
        return PostMetadata()

    author = None
    owner = node.get("user") or node.get("owner")
    if isinstance(owner, dict) and isinstance(owner.get("username"), str):
        author = owner["username"]

    caption = None
    raw_caption = node.get("caption")
    if isinstance(raw_caption, dict) and isinstance(raw_caption.get("text"), str):
        caption = raw_caption["text"]
    elif isinstance(raw_caption, str):
        caption = raw_caption

    timestamp = None
    taken_at = _int_or_none(node.get("taken_at"))
    if taken_at:
        timestamp = datetime.fromtimestamp(taken_at, tz=timezone.utc).isoformat()

    code = node.get("code")
    return PostMetadata(
        code=code if isinstance(code, str) and code else None,
        author=author,
        caption=caption,
        timestamp=timestamp,
    )


def find_media_nodes(payload: Any) -> list[dict[str, Any]]:
    """Locate media nodes by key presence, in the §5.4 step-3 order.

    Carousel first: a carousel container ALSO carries the child's keys at the
    top level on some payload shapes, so checking `video_versions` first
    would return one item for a thirteen-item post.
    """
    for node in walk_objects(payload):
        carousel = node.get("carousel_media")
        if isinstance(carousel, list) and carousel:
            return [child for child in carousel if isinstance(child, dict)]

    for key in ("video_versions", "image_versions2"):
        for node in walk_objects(payload):
            if node.get(key):
                return [node]
    return []


#: A login wall is a page that ASKS for a password. Detecting it by the
#: presence of the string "/accounts/login" does not work and is not a close
#: call: measured on the first two real captures (2026-08-16), an ordinary
#: logged-out post page carries that string 3 times in inert navigation while
#: serving 91 media variants. A substring test there rejects every real page.
_PASSWORD_INPUT_RE = re.compile(r"""<input[^>]+type=["']?password""", re.IGNORECASE)
_LOGIN_FORM_RE = re.compile(r"""<form[^>]+action=["'][^"']*\/accounts\/login""", re.IGNORECASE)


def looks_like_login_wall(html: str) -> bool:
    """True when the page is asking the user to log in.

    Keyed on a rendered password field or a form posting to the login
    endpoint — the things that only exist when Instagram is actually
    demanding credentials. Navigation links to the login page are present on
    every logged-out page and mean nothing.
    """
    return bool(_PASSWORD_INPUT_RE.search(html) or _LOGIN_FORM_RE.search(html))


def iter_dash_manifests(html: str) -> list[str]:
    """Every `video_dash_manifest` string on the page, decoded.

    Goes through the same payload walk as everything else rather than
    scanning the raw text for the key. A hand-rolled scan has to guess where
    the JSON string ends, and it guesses wrong the moment the surrounding
    shape changes -- which is the one thing this page is guaranteed to do.
    """
    found: list[str] = []
    for payload in iter_script_payloads(html):
        for node in walk_objects(payload):
            manifest = node.get("video_dash_manifest")
            if isinstance(manifest, str) and manifest.strip():
                found.append(manifest)
    return found


@dataclass(frozen=True)
class ExtractedPost:
    """A page's media, plus what was kept out of the menu and why."""

    items: list[MediaItem]
    excluded: list[ExcludedFormats]


def extract_post(html: str) -> ExtractedPost:
    """Run the §5.4 algorithm over a page's HTML.

    Returns items in carousel order. Raises nothing on an empty result — the
    caller decides whether empty is a structure change, because only it knows
    whether the page actually loaded (see `extract_post_or_raise`).
    """
    for payload in iter_script_payloads(html):
        nodes = find_media_nodes(payload)
        if not nodes:
            continue
        built = [_build_item(node, index) for index, node in enumerate(nodes)]
        items = [item for item, _crops in built if item]
        if not items:
            continue
        crops = sum(count for _item, count in built)
        excluded = (
            [ExcludedFormats(reason="square_crop_not_rendition", count=crops)] if crops else []
        )
        return ExtractedPost(items=items, excluded=excluded)
    return ExtractedPost(items=[], excluded=[])


def extract_items(html: str) -> list[MediaItem]:
    """`extract_post(html).items` -- the media alone."""
    return extract_post(html).items


def extract_post_or_raise(html: str, *, shortcode: str) -> ExtractedPost:
    """§5.4 step 9: never return empty silently.

    A page that loaded and names the post but yields no media is the signal
    that the payload shape moved. Reporting it as "no media" would send the
    user hunting for a problem with their link.
    """
    post = extract_post(html)
    if post.items:
        return post
    raise UpstreamStructureChange(
        f"page for {shortcode} loaded but no media nodes were found. Either the "
        "payload structure changed, or every media URL failed the oh=/oe= "
        "signature check (TRAP-4).",
        shortcode=shortcode,
        html_length=len(html),
    )


def extract_items_or_raise(html: str, *, shortcode: str) -> list[MediaItem]:
    """`extract_post_or_raise(...).items` -- the media alone."""
    return extract_post_or_raise(html, shortcode=shortcode).items


__all__ = [
    "SEGMENTED_ADDRESSING_TAGS",
    "iter_dash_manifests",
    "looks_like_login_wall",
    "DashRepresentation",
    "balanced_object_slices",
    "dash_unavailable",
    "parse_dash_manifest",
    "REQUIRED_SIGNATURE_PARAMS",
    "UI_ASSET_HOSTS",
    "build_item",
    "PostMetadata",
    "dash_audio_variant",
    "dash_variants",
    "find_post_node",
    "post_metadata",
    "expires_at_from_url",
    "ExtractedPost",
    "ImageCandidates",
    "StpTransform",
    "classify_image_candidates",
    "extract_items",
    "extract_items_or_raise",
    "extract_post",
    "extract_post_or_raise",
    "original_size_from_crop",
    "parse_stp",
    "find_media_nodes",
    "height_from_tag",
    "image_variants",
    "is_signed_media_url",
    "iter_script_payloads",
    "progressive_video_variants",
    "quality_tag",
    "unescape_script_json",
    "walk_objects",
]
