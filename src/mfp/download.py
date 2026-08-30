"""Media transfer engine (PSM Batch 1 core §12 M4).

Takes a Manifest whose items already carry a `chosen` Variant and turns it
into files on disk. Three properties define the module:

**Atomic.** Bytes land in a `.part` file and are `os.replace`d into place
only once the transfer is verified complete. A finished file in the output
tree is therefore always whole; there is no state in which the user's folder
contains a half-written video that looks finished.

**Resumable across processes.** Queue INV-4 demotes live tasks to `PAUSED`
on load, so "resume" routinely means a *different process* picking up a
`.part` it did not write. That is only safe with provenance, so every
`.part` carries a `.part.meta` sidecar naming the asset and its expected
size. Anything that does not match is discarded and re-fetched: appending
fresh bytes to the wrong partial file produces a corrupt output that looks
perfectly fine, which is the failure mode this codebase is organised
against (TRAP-4 is the same shape).

**Muxing, not stitching.** D-29 measured every DASH representation on both
Instagram and Threads as `single_file`, so no segment assembly is needed --
but those representations are video-only, so the highest quality always
arrives as two transfers plus one ffmpeg pass. For a muxed item the layout
beside the destination `<name>` is:

    <name>.v.part / <name>.v.part.meta    video-only stream
    <name>.a.part / <name>.a.part.meta    audio-only stream
    <name>.part                           ffmpeg output, renamed to <name>

**This module does not touch the fetch budget governor, deliberately.**
PSM §4.5 says every outbound network action goes through
`ctx.budget.acquire()`; that clause predates §5.6, which measured plain
`httpx` against fbcdn and stated the transfer layer may parallelize and
resume freely. The governor counts "how many times did we poke Meta's page
endpoints this hour" -- the surface bot detection actually watches. Feeding
file transfers into that same window would not merely slow downloads: it
would corrupt the safety instrument, making the pacing guard believe it had
spent its hour while downloading one large carousel. §4.5 carries a scope
note recording this (2026-08-17).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from collections import deque
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx

from mfp import logs
from mfp.errors import (
    DependencyMissingError,
    LinkExpired,
    MediaTransferFailedError,
    PlatformTransferBlocked,
    RateLimitedError,
)
from mfp.models import (
    FetchResult,
    FetchResultBudget,
    FetchResultItem,
    Manifest,
    MediaItem,
    Variant,
)
from mfp.naming import info_filename, manifest_filename, media_filename, resolve_output_path
from mfp.policy import resolution_class

#: 256 KiB. Large enough that per-chunk overhead is noise on a 50 MB Reel,
#: small enough that a cancel is noticed promptly.
CHUNK_BYTES = 256 * 1024

#: How much of the asset one HTTP request asks for. Distinct from
#: `CHUNK_BYTES`, which is how much is read from the socket at a time.
#:
#: googlevideo paces an open-ended GET at roughly playback rate -- deliberate,
#: so a player cannot buffer far ahead -- and a bounded range is served at
#: line speed instead. Measured on one 54 MB video-only stream, same URL and
#: same client back to back:
#:
#:     open-ended GET   8 MB in 161.9 s  =     50.6 KB/s
#:     bounded ranges  10 MB in   0.25 s = 41,487.1 KB/s
#:
#: 656x, and it is why a YouTube fetch used to sit at ~35 KB/s while
#: `yt-dlp` pulled the same stream at 18 MB/s. fbcdn does not pace this way,
#: so Instagram was never affected and is not changed by this.
HTTP_CHUNK_BYTES = 10 * 1024 * 1024

#: Progress events are rate-limited here as well as in the server's
#: broadcaster, because this callback also feeds the CLI's stderr renderer,
#: which has no coalescing of its own.
PROGRESS_INTERVAL_SECONDS = 0.2

#: Window over which `bytesPerSec` is averaged. Instantaneous chunk rates
#: swing wildly enough to make an ETA useless.
SPEED_WINDOW_SECONDS = 3.0

MAX_ATTEMPTS = 3

#: Announcing `python-httpx/x.y` on every transfer is a free identifying
#: signal, and the user's standing instruction is to keep bot protection
#: real (D-33). fbcdn was measured to serve these assets with no headers at
#: all (spike-02 §3), so this is not required to *work* -- it is here so the
#: transfer looks like the browser that legitimately loaded the same page.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

_PART_SUFFIX = ".part"
_SIDECAR_SUFFIX = ".meta"
_VIDEO_STREAM_SUFFIX = ".v"
_AUDIO_STREAM_SUFFIX = ".a"

TransferPhase = Literal["transferring", "muxing"]
StreamRole = Literal["media", "video", "audio"]


class TransferCancelled(Exception):
    """Raised inside a transfer when the caller's cancel predicate trips.

    Not an `MfpError`: a cancellation is not a taxonomy failure, it is the
    user getting what they asked for. It surfaces on the wire as
    `FetchResult.stopReason == "user_cancelled"`.
    """


@dataclass(frozen=True)
class ProgressEvent:
    """One progress sample for one item.

    `phase` is the field that answers M4's open design question. A muxed
    item is two transfers plus an ffmpeg pass, so a byte counter alone goes
    to 100% and then sits there while ffmpeg works -- the bar says finished
    and the file does not exist yet. `bytes_*` describe the transfers only
    and never move during `muxing`; the phase is what tells the UI that
    work continues. The mux itself is reported without a percentage on
    purpose: it is a stream copy, measured in seconds, and a percentage
    would need a media duration this build does not parse. If it ever gets
    slow, `ffmpeg -progress pipe:1` is the named upgrade.
    """

    item_index: int
    phase: TransferPhase
    stream: StreamRole
    bytes_done: int
    bytes_total: int | None
    bytes_per_sec: float
    eta_seconds: float | None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class TransferPlan:
    """Everything the engine needs that is not the item itself.

    Kept separate from `FetchContext` so `download.py` stays free of adapter
    concerns: it moves bytes and writes files, while the adapter supplies
    identity and the budget block.
    """

    output_root: Path
    platform: str
    author: str | None
    date: str
    post_id: str
    ffmpeg: str | None = None
    #: The caller's answer to "there is no sound anywhere for this item".
    #: False -- the default -- keeps `_download_muxed`'s refusal to write a
    #: file with no sound. True is only ever set because someone asked for it
    #: after being told, and the policy layer has already declared the loss
    #: (`SILENT_VIDEO_REASON`). It does NOT loosen the missing-muxer case:
    #: there the sound exists and the fix is to install ffmpeg.
    allow_silent_video: bool = False


# --- part-file provenance ---------------------------------------------------


def asset_key(url: str) -> str:
    """The stable identity of a CDN asset, for resume checks.

    The full URL is unusable as a key: its signature (`oh=`/`oe=`) rotates
    on every probe, so a legitimate resume after a re-probe would never
    match. The host is unusable too -- the same asset is served from
    whichever `scontent-<edge>` node answered -- and keying on it would
    discard good partial files. The path carries the asset hash and is
    stable across both.

    The path ALONE is not enough, though (ADR-B2, measured 2026-08-25). All
    thirteen Instagram renditions of one photo share one path and differ only
    by `stp`, the server-side transform: a 1122x1402 original and a 720x900
    scale of it are the same path. The transform therefore joins the key. It
    is safe to include -- unlike the signature it does not rotate between
    probes, and unlike the host it does not vary by edge.

    Without it the corruption is SIZE-CORRECT and therefore invisible:
    interrupt `--policy best` partway, run `--policy max-height:720`, and the
    resume appends the small rendition onto a prefix of the large one, landing
    on exactly the small rendition's true length.

    A `.part` written before this change carries a path-only key and will now
    be discarded rather than continued. That costs a re-transfer once, which
    is the direction this module always takes when provenance is in doubt.
    """
    split = urlsplit(url)
    transform = parse_qs(split.query).get("stp", [""])[0]
    return f"{split.path}?stp={transform}" if transform else split.path


def _sidecar_path(part_path: Path) -> Path:
    return part_path.with_name(part_path.name + _SIDECAR_SUFFIX)


def _write_sidecar(part_path: Path, *, url: str, expected_total: int | None) -> None:
    """Written *before* the first byte, not after the last.

    A sidecar written on completion would be absent exactly when it is
    needed -- after a crash mid-transfer.
    """
    payload = {"schemaVersion": 1, "assetKey": asset_key(url), "expectedTotal": expected_total}
    _sidecar_path(part_path).write_text(json.dumps(payload), encoding="utf-8")


def _discard_part(part_path: Path) -> None:
    for path in (part_path, _sidecar_path(part_path)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def resume_offset(part_path: Path, *, url: str, expected_total: int | None) -> int:
    """How many bytes of `part_path` may be kept. Discards it if any doubt.

    Every branch that returns 0 also deletes the partial file, so a caller
    can open in append mode on a non-zero result and truncating mode
    otherwise without a second existence check.
    """
    if not part_path.exists():
        _discard_part(part_path)  # sweep an orphaned sidecar
        return 0

    try:
        raw = json.loads(_sidecar_path(part_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A `.part` with no readable provenance could have come from any
        # asset. Re-fetching costs bandwidth; appending to it costs the
        # user a corrupt file they will not discover until playback.
        _discard_part(part_path)
        return 0

    if not isinstance(raw, dict) or raw.get("assetKey") != asset_key(url):
        _discard_part(part_path)
        return 0

    recorded_total = raw.get("expectedTotal")
    if expected_total is not None and recorded_total not in (None, expected_total):
        _discard_part(part_path)
        return 0

    size = part_path.stat().st_size
    limit = expected_total if expected_total is not None else recorded_total
    if isinstance(limit, int) and size >= limit:
        # `size > limit` is corrupt outright. `size == limit` is the
        # microsecond window between the final write and `os.replace`, and
        # a re-fetch is the only way to be sure the bytes are what the
        # sidecar says they are.
        _discard_part(part_path)
        return 0
    return size


# --- progress ---------------------------------------------------------------


@dataclass
class _ProgressPump:
    """Throttles callbacks and averages speed over a short window.

    Callers report the **absolute** byte count of the stream they are
    moving, never a delta, and the pump derives the item-level aggregate.
    Deltas were the first design and they were wrong twice over: a retry
    that resumed from 500 added those 500 a second time, and the
    "server ignored our Range" path had to subtract them back out. With an
    absolute per-stream figure both cases are ordinary assignments.
    """

    item_index: int
    on_progress: ProgressCallback | None
    monotonic: Callable[[], float]
    bytes_total: int | None = None
    bytes_done: int = 0
    _stream_bytes: dict[str, int] = field(default_factory=dict)
    _samples: deque[tuple[float, int]] = field(default_factory=deque)
    _last_emit: float | None = None

    def set_stream_bytes(self, stream: StreamRole, value: int, *, force: bool = False) -> None:
        self.bytes_done += value - self._stream_bytes.get(stream, 0)
        self._stream_bytes[stream] = value
        now = self.monotonic()
        self._samples.append((now, self.bytes_done))
        while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW_SECONDS:
            self._samples.popleft()
        if not force and self._last_emit is not None:
            if now - self._last_emit < PROGRESS_INTERVAL_SECONDS:
                return
        self._last_emit = now
        self._emit("transferring", stream)

    def muxing(self) -> None:
        self._last_emit = self.monotonic()
        self._emit("muxing", "video")

    def _emit(self, phase: TransferPhase, stream: StreamRole) -> None:
        if self.on_progress is None:
            return
        speed = self._speed()
        remaining = None
        if self.bytes_total is not None:
            remaining = max(0, self.bytes_total - self.bytes_done)
        eta = remaining / speed if remaining is not None and speed > 0 else None
        self.on_progress(
            ProgressEvent(
                item_index=self.item_index,
                phase=phase,
                stream=stream,
                bytes_done=self.bytes_done,
                bytes_total=self.bytes_total,
                bytes_per_sec=speed,
                eta_seconds=eta,
            )
        )

    def _speed(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        (start_time, start_bytes), (end_time, end_bytes) = self._samples[0], self._samples[-1]
        elapsed = end_time - start_time
        return (end_bytes - start_bytes) / elapsed if elapsed > 0 else 0.0


# --- HTTP -------------------------------------------------------------------


def build_client(*, user_agent: str = DEFAULT_USER_AGENT) -> httpx.Client:
    """The default transfer client.

    `follow_redirects` is on because fbcdn does hand out 302s between edge
    nodes. Timeouts are split: a slow *connect* is a dead edge and should
    fail fast, while a slow *read* on a large file is ordinary.
    """
    return httpx.Client(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
    )


#: Hosts measured to refuse a transfer the platform itself had just
#: authorised. A 403 from one of these is a platform ruling rather than a
#: mangled link, and telling the reader to retry would send them looking for
#: a fault on their own machine that is not there.
_TRANSFER_BLOCKING_HOSTS: tuple[str, ...] = (".googlevideo.com",)


def _blocks_transfers(url: str) -> bool:
    host = (httpx.URL(url).host or "").lower()
    return any(host.endswith(suffix) for suffix in _TRANSFER_BLOCKING_HOSTS)


def _raise_for_transfer_status(response: httpx.Response, url: str) -> None:
    """Turn a refused transfer into the taxonomy's own error.

    The URL is redacted through `logs.safe_url` on the way in. These messages
    reach three places that outlive the moment -- the terminal, the error
    bundle on disk, and (since `mfp brief`) an AI agent's own tool output,
    which gets pasted into bug reports and fed back into later turns. A signed
    CDN URL is a short-lived credential for somebody's bytes, and `safe_url`
    already existed for exactly this reason; it was simply never applied here.
    The path still identifies which asset failed, which is the whole
    diagnostic value of naming it.
    """
    safe = logs.safe_url(url)
    if response.status_code == 429:
        raise RateLimitedError(f"the CDN returned 429 for {safe}", url=safe)
    if response.status_code == 403 and _blocks_transfers(url):
        raise PlatformTransferBlocked(
            f"the platform refused the transfer with HTTP 403 for {safe}; "
            "the link has not expired and this is not retryable -- yt-dlp "
            "itself is refused the same way (O-11)"
        )
    if response.status_code >= 400:
        detail = (
            "; the signature may be entity-mangled (TRAP-4) or expired"
            if response.status_code == 403
            else ""
        )
        raise MediaTransferFailedError(
            f"the CDN returned HTTP {response.status_code} for {safe}{detail}"
        )


def _content_range_start(response: httpx.Response) -> int | None:
    """First byte offset a 206 says it is delivering, or None if it did not say.

    Worth checking rather than assuming. A server can take the `Range` header
    and still answer from byte zero -- and once the transfer is a LOOP of
    ranged requests, believing it would append the same prefix again and
    again until the byte count reached the promised total, producing a
    corrupt file with a perfectly normal name.
    """
    content_range = response.headers.get("content-range", "")
    head = content_range.removeprefix("bytes").strip().split("/", 1)[0]
    start = head.split("-", 1)[0].strip()
    return int(start) if start.isdigit() else None


def _total_from_response(response: httpx.Response, offset: int) -> int | None:
    """Total size of the whole asset, not of this response's body."""
    content_range = response.headers.get("content-range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("content-length")
    if length and length.isdigit():
        return int(length) + (offset if response.status_code == 206 else 0)
    return None


def probe_total_bytes(
    url: str, *, client: httpx.Client, headers: dict[str, str] | None = None
) -> int | None:
    """Total size of an asset, via a one-byte ranged GET.

    Used before a muxed item starts so `bytesTotal` is the true two-stream
    sum from the first progress event -- otherwise the bar reaches 100% on
    the video and then jumps backwards when the audio begins.

    It earns its cost a second way: a 403 on the audio track surfaces here,
    before a 50 MB video transfer, instead of after it. A ranged GET rather
    than HEAD because a 206 with `Content-Range` is what fbcdn was actually
    measured to serve (spike-02 §3); HEAD is untested against it.
    """
    try:
        response = client.get(url, headers={**(headers or {}), "Range": "bytes=0-0"})
    except httpx.HTTPError:
        # An unknown total is survivable -- progress reports None and the
        # transfer proceeds. Only a *verdict* (4xx below) is worth raising.
        return None
    _raise_for_transfer_status(response, url)
    return _total_from_response(response, 0)


#: How much of an image to pull when measuring it. The JPEG SOF marker that
#: carries the dimensions sits after every APP segment, and a large EXIF or
#: ICC block can push it well past the 2 KB this was first specified at. 32 KB
#: clears that with room to spare and is still a rounding error next to the
#: image itself -- and on the measured Instagram shape this path never runs at
#: all, because `stp` already answered (D-93).
IMAGE_HEADER_BYTES = 32 * 1024


def measured_size(
    url: str, *, client: httpx.Client, headers: dict[str, str] | None = None
) -> tuple[int, int] | None:
    """`(width, height)` read from the first bytes of the image itself.

    The fallback for a size the URL would not declare. A ranged GET costs
    bandwidth and no rate budget (D-34), so this is a bandwidth decision
    rather than a pacing one.

    **Never raises**, which is the difference from `probe_total_bytes` beside
    it. That one guards a transfer about to happen and a 403 there is a
    verdict worth stopping for; this one runs over every unresolved candidate
    of a post at probe time, where one refused thumbnail must not fail the
    whole probe. Every failure -- transport, 4xx, truncated stream, bytes that
    are not an image -- returns None, and an unresolved size stays None rather
    than becoming a guess.
    """
    try:
        # Imported here, not at module scope: `stack` may raise on a Pillow
        # that will not import, because without it `stack` cannot run at all.
        # This module is on the path of every `fetch`, and a broken Pillow
        # must cost one unresolved size, not the whole command.
        from PIL import Image

        prefix = _image_prefix(url, client=client, headers=headers)
        if prefix is None:
            return None
        with Image.open(io.BytesIO(prefix)) as image:
            width, height = image.size
    except (httpx.HTTPError, OSError, ValueError, ModuleNotFoundError):
        # `Image.open` raises UnidentifiedImageError (an OSError) on garbage
        # and on a prefix too short to carry the header.
        return None
    return (width, height) if width and height else None


def _image_prefix(
    url: str, *, client: httpx.Client, headers: dict[str, str] | None
) -> bytes | None:
    """At most `IMAGE_HEADER_BYTES` from the front of `url`.

    Streamed and capped rather than read whole. A CDN that ignores `Range`
    answers 200 with the ENTIRE image, and this function runs over every
    unresolved candidate of a post -- buffering all of them is how a
    diagnostic becomes the expensive part of a probe.

    The redirect check is the other half. `build_client` follows redirects
    because fbcdn genuinely moves between edge nodes, but an edge failover
    keeps the asset's path while a substituted placeholder does not. Without
    the check, a "content unavailable" image answered with 200 would be
    parsed and its dimensions reported as this rendition's -- a confident
    wrong number where the doctrine calls for None.
    """
    request_path = httpx.URL(url).path
    with client.stream(
        "GET", url, headers={**(headers or {}), "Range": f"bytes=0-{IMAGE_HEADER_BYTES - 1}"}
    ) as response:
        if response.status_code not in (200, 206):
            return None
        if response.url.path != request_path:
            return None
        chunks = bytearray()
        for chunk in response.iter_bytes(IMAGE_HEADER_BYTES):
            chunks += chunk
            if len(chunks) >= IMAGE_HEADER_BYTES:
                break
    return bytes(chunks)


def transfer_stream(
    url: str,
    destination: Path,
    *,
    client: httpx.Client,
    pump: _ProgressPump | None = None,
    stream: StreamRole = "media",
    cancel: Callable[[], bool] | None = None,
    expected_total: int | None = None,
    chunk_size: int = CHUNK_BYTES,
    headers: dict[str, str] | None = None,
) -> int:
    """Transfer one URL to `destination`, resuming and renaming atomically.

    Returns the byte size of the finished file. `destination` is the final
    path; this function owns `<destination>.part` and its sidecar.

    `headers` are what the SOURCE said this URL needs (`Variant`
    `requestHeaders`), merged under our own `Range`. Some CDNs refuse
    without them -- Bilibili's `bilivideo.com` mirrors want a `Referer` --
    and the refusal is a plain 403 that looks exactly like an expired
    signature, so omitting them fails in the most misleading way available.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(destination.name + _PART_SUFFIX)

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        offset = resume_offset(part_path, url=url, expected_total=expected_total)
        if pump is not None:
            pump.set_stream_bytes(stream, offset)
        try:
            return _transfer_once(
                url,
                destination,
                part_path,
                offset=offset,
                client=client,
                pump=pump,
                stream=stream,
                cancel=cancel,
                chunk_size=chunk_size,
                headers=headers,
            )
        except TransferCancelled:
            raise
        except (httpx.TransportError, httpx.RemoteProtocolError) as error:
            # Transient by nature, and resume makes the retry nearly free:
            # the next attempt picks up from whatever landed.
            last_error = error
            if attempt == MAX_ATTEMPTS:
                break
        except MediaTransferFailedError as error:
            # A 4xx is a verdict about the URL, not a hiccup. Retrying a
            # mangled or expired signature just spends requests.
            raise error

    raise MediaTransferFailedError(
        f"{url} failed after {MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _transfer_once(
    url: str,
    destination: Path,
    part_path: Path,
    *,
    offset: int,
    client: httpx.Client,
    pump: _ProgressPump | None,
    stream: StreamRole,
    cancel: Callable[[], bool] | None,
    chunk_size: int,
    headers: dict[str, str] | None = None,
    http_chunk_size: int = HTTP_CHUNK_BYTES,
) -> int:
    # One BOUNDED ranged request per pass, rather than one open-ended GET for
    # the whole asset. See HTTP_CHUNK_BYTES for why: an unbounded GET is
    # paced by googlevideo at roughly playback rate.
    #
    # Source headers first, our Range last: the transfer owns the Range and
    # a source must never be able to narrow it (that would truncate a
    # resume into a file that still parses).
    written = offset
    total: int | None = None
    ranged = True

    while True:
        request_headers = dict(headers or {})
        request_headers["Range"] = f"bytes={written}-{written + http_chunk_size - 1}"
        with client.stream("GET", url, headers=request_headers) as response:
            if response.status_code == 416 and written > offset:
                # Asked past the end. Only reachable when no header ever
                # stated the total, so nothing else could stop the loop.
                break
            # A 4xx stays terminal here, deliberately. A server that dislikes
            # our Range is hypothetical; a mangled or expired signature is
            # not, and retrying that just spends requests.
            _raise_for_transfer_status(response, url)
            if response.status_code != 206:
                # The server ignored the Range and is sending the whole body.
                # Appending it would duplicate the prefix; start over.
                ranged = False
                written = 0
                if pump is not None:
                    pump.set_stream_bytes(stream, 0)
            if ranged and _content_range_start(response) not in (None, written):
                # It answered from somewhere other than where we asked. Stop
                # before writing: appending this would duplicate a prefix.
                # Whatever has landed is then judged by the size check below,
                # which is the same verdict a short stream has always got.
                break

            if total is None:
                total = _total_from_response(response, written)
                if pump is not None and pump.bytes_total is None and total is not None:
                    pump.bytes_total = total
                _write_sidecar(part_path, url=url, expected_total=total)

            before = written
            with part_path.open("ab" if written else "wb") as handle:
                for chunk in response.iter_bytes(chunk_size):
                    if cancel is not None and cancel():
                        handle.flush()
                        raise TransferCancelled(url)
                    handle.write(chunk)
                    written += len(chunk)
                    if pump is not None:
                        pump.set_stream_bytes(stream, written)

        if not ranged:
            break
        if total is not None and written >= total:
            break
        if written == before:
            # A range that delivered nothing. Without this the loop would
            # spin on an asset whose size no header ever stated, and on the
            # short-stream case below it is what lets the size check speak.
            break

    if total is not None and written != total:
        # A stream that ends early is the silent-lie case: renaming it would
        # put a truncated file in the user's folder with a normal name.
        _discard_part(part_path)
        raise MediaTransferFailedError(
            f"{url} delivered {written} of {total} bytes; the transfer ended early"
        )

    os.replace(part_path, destination)
    try:
        _sidecar_path(part_path).unlink(missing_ok=True)
    except OSError:
        pass
    if pump is not None:
        pump.set_stream_bytes(stream, written, force=True)
    return written


# --- mux --------------------------------------------------------------------


#: Destination extension -> the ffmpeg muxer name for it.
_FFMPEG_FORMATS: dict[str, str] = {".mp4": "mp4", ".m4a": "mp4", ".webm": "webm"}


def mux_streams(
    video_path: Path,
    audio_path: Path,
    destination: Path,
    *,
    ffmpeg: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Join a video-only and an audio-only file with a stream copy.

    `-c copy` because both tracks are already in the codecs we want;
    re-encoding would cost minutes and quality for nothing.
    `+faststart` moves the moov atom to the front so the result seeks
    immediately in a player, which is the whole point of downloading it.

    `-f` is explicit and it is not optional. ffmpeg picks its output muxer
    from the filename extension, and this writes to `<name>.mp4.part` for
    atomicity -- so inference sees `.part`, knows no such format, and dies
    with `Invalid argument`. Measured on the first live run, 2026-08-17,
    after both streams had transferred perfectly: the whole test suite was
    green because every test in it supplied its own mux callable, and the
    real ffmpeg had never been asked to write to a real `.part`.
    """
    part_path = destination.with_name(destination.name + _PART_SUFFIX)
    container = _FFMPEG_FORMATS.get(destination.suffix.lower(), "mp4")
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        *(["-movflags", "+faststart"] if container == "mp4" else []),
        "-f",
        container,
        str(part_path),
    ]
    # argv array, never a shell string (Phase 2 §4.2): these paths contain
    # remote-derived components. `stdin=DEVNULL` because ffmpeg reads stdin
    # for interactive commands: inheriting ours would let it consume the very
    # bytes `serve`'s parent-death watcher is blocked on. Measured 2026-08-17
    # that a real mux does NOT currently hang under that watcher -- this is
    # the class being closed, not a reproduced failure (the reproduced one is
    # in `doctor._default_runner`).
    started = time.monotonic()
    completed = runner(
        argv, shell=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    logs.ran(ffmpeg, args=["mux", "->", destination.name],
             code=completed.returncode,
             ms=int((time.monotonic() - started) * 1000))
    if completed.returncode != 0:
        part_path.unlink(missing_ok=True)
        detail = (completed.stderr or "").strip().splitlines()
        error = MediaTransferFailedError(
            f"ffmpeg failed to mux {destination.name} (exit {completed.returncode}): "
            + (detail[-1] if detail else "no stderr")
        )
        # The bundle takes the whole stderr; the message keeps its one line.
        error.log_command = list(argv)
        error.log_stderr = completed.stderr
        raise error
    os.replace(part_path, destination)


# --- expiry -----------------------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def check_not_expired(variant: Variant, *, post_url: str, now: datetime) -> None:
    """Refuse a dead signature before spending a transfer (PSM §5.7).

    An unknown expiry is not an expiry: `expiresAt` is None whenever `oe=`
    could not be decoded, and refusing on that would block perfectly good
    URLs.
    """
    expires = _parse_iso(variant.expires_at)
    if expires is not None and expires <= now:
        raise LinkExpired(
            f"the signed URL for {post_url} expired at {variant.expires_at}; "
            "re-probe the post to get a fresh signature"
        )


# --- item -------------------------------------------------------------------


def _date_from_manifest(manifest: Manifest, *, now: datetime) -> str:
    stamp = _parse_iso(manifest.source.timestamp)
    return (stamp or now).strftime("%Y-%m-%d")


def _destination_for(item: MediaItem, variant: Variant, plan: TransferPlan) -> Path:
    # `resolution_class`, not `height`: the suffix is the rung a person would
    # name, and a portrait Reel's height is its LONG side -- 1080x1920 was
    # landing as `_1920p.mp4`, a number no menu anywhere offers. Same ruling
    # as `max-height` (2026-08-17), applied to the other place it shows.
    filename = media_filename(
        plan.post_id,
        item.index,
        variant.ext,
        rung=resolution_class(variant) if item.kind == "video" else None,
    )
    return resolve_output_path(
        plan.output_root,
        platform=plan.platform,
        author=plan.author,
        date=plan.date,
        post_id=plan.post_id,
        filename=filename,
    )


def nothing_choosable_detail(item: MediaItem) -> str:
    """Why the policy engine had nothing to hand over.

    Both endings carry the same code -- this build cannot complete any
    rendition of this item -- but they send the reader to different places,
    and the ffmpeg wording is a wasted install when the muxer is already
    there. Video-only renditions with `audio is None` means the manifest
    never recorded the sound, which no local dependency can supply.
    """
    if item.audio is None and any(variant.needs_mux for variant in item.variants):
        return (
            "no variant of this item can be turned into a playable file: its renditions "
            "are video-only bytes and the manifest records no audio track to join them "
            "to; re-probe the post, and if it recurs the audio adaptation moved. To take "
            "the picture without sound instead, re-run with --allow-silent-video"
        )
    return (
        "no variant of this item can be turned into a playable file on this "
        "machine; the higher renditions need ffmpeg to mux and it was not found"
    )


def download_item(
    item: MediaItem,
    plan: TransferPlan,
    *,
    client: httpx.Client,
    post_url: str,
    now: datetime,
    on_progress: ProgressCallback | None = None,
    cancel: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    mux: Callable[..., None] = mux_streams,
) -> FetchResultItem:
    """Transfer one item's chosen variant, muxing when it needs audio.

    Raises only `TransferCancelled`; every other failure is folded into a
    `failed` row, because one dead item in a thirteen-item carousel must not
    cost the user the other twelve.
    """
    chosen = item.chosen
    if chosen is None:
        return FetchResultItem(
            index=item.index,
            status="failed",
            error_code=DependencyMissingError.error_code,
            error_detail=nothing_choosable_detail(item),
        )

    destination = _destination_for(item, chosen, plan)
    pump = _ProgressPump(item_index=item.index, on_progress=on_progress, monotonic=monotonic)

    # A `needs_mux` rendition with nothing to mux it with reaches here only
    # when the run asked for a silent video: `policy._selectable` refuses to
    # choose one otherwise, and `_download_muxed` refuses to write one. There
    # is no second stream to join, so the muxer is not involved at all --
    # this is one URL, transferred as it is.
    silent_video = chosen.needs_mux and item.audio is None and plan.allow_silent_video

    try:
        check_not_expired(chosen, post_url=post_url, now=now)
        if not chosen.needs_mux or silent_video:
            size = transfer_stream(
                chosen.url,
                destination,
                client=client,
                pump=pump,
                stream="media",
                cancel=cancel,
                headers=chosen.request_headers,
            )
        else:
            size = _download_muxed(
                item,
                chosen,
                destination,
                plan,
                client=client,
                post_url=post_url,
                now=now,
                pump=pump,
                cancel=cancel,
                mux=mux,
            )
    except TransferCancelled:
        raise
    except Exception as error:  # noqa: BLE001 -- folded into the result row
        code = getattr(error, "error_code", MediaTransferFailedError.error_code)
        return FetchResultItem(
            index=item.index,
            status="failed",
            chosen=chosen,
            error_code=code,
            error_detail=str(error),
        )

    _save_sidecars(item, destination, client=client)

    return FetchResultItem(
        index=item.index,
        status="ok",
        path=str(destination),
        size_bytes=size,
        chosen=chosen,
    )


def _save_sidecars(item: MediaItem, destination: Path, *, client: httpx.Client) -> None:
    """Save each sidecar beside the media, under the same stem.

    Runs AFTER the media landed and never raises. A sidecar is an extra, and
    the item it belongs to already succeeded -- failing the whole download
    because a comments file did not arrive would trade a video the user
    asked for against a file they did not.

    The failure is not silent either: nothing is announced when it works,
    and a `.danmaku.xml` that is simply absent is the honest report of an
    extra that could not be fetched.
    """
    for sidecar in item.sidecars:
        target = destination.with_name(f"{destination.stem}.{sidecar.kind}.{sidecar.ext}")
        try:
            transfer_stream(
                sidecar.url,
                target,
                client=client,
                headers=sidecar.request_headers,
            )
        except Exception:  # noqa: BLE001 -- see the docstring
            continue


def _fetch_stream(
    url: str,
    target: Path,
    role: StreamRole,
    total: int | None,
    client: httpx.Client,
    pump: _ProgressPump,
    cancel: Callable[[], bool] | None,
    headers: dict[str, str] | None = None,
) -> None:
    """Transfer one half of a muxed item, reusing an intact earlier copy.

    A `.v`/`.a` intermediate only ever comes into existence through
    `os.replace` from a `.part` whose length was checked against the
    server's, so unlike a `.part` it is *verified* -- reusing it is not the
    "trust a file because it is the right size" mistake `resume_offset`
    refuses to make. It matters because the first live run failed at the
    mux with both streams already down: without this, retrying a
    ten-megabyte Reel re-transfers ten megabytes to redo a two-second
    stream copy.
    """
    if target.exists() and total is not None and target.stat().st_size == total:
        pump.set_stream_bytes(role, total)
        return
    transfer_stream(
        url,
        target,
        client=client,
        pump=pump,
        stream=role,
        cancel=cancel,
        expected_total=total,
        headers=headers,
    )


def _download_muxed(
    item: MediaItem,
    chosen: Variant,
    destination: Path,
    plan: TransferPlan,
    *,
    client: httpx.Client,
    post_url: str,
    now: datetime,
    pump: _ProgressPump,
    cancel: Callable[[], bool] | None,
    mux: Callable[..., None],
) -> int:
    audio = item.audio
    if audio is None:
        # `needs_mux` is a promise that a second stream exists. If the
        # extractor did not record one, the promise is broken and the honest
        # move is to say so -- not to write a silent video with no sound.
        raise MediaTransferFailedError(
            f"item {item.index} needs muxing but the manifest carries no audio track; "
            "re-probe the post, and if it recurs the DASH audio adaptation moved"
        )
    if not plan.ffmpeg:
        raise DependencyMissingError(
            "ffmpeg is required to join this rendition's separate video and audio "
            "tracks and was not found (PSM §14.1)"
        )
    check_not_expired(audio, post_url=post_url, now=now)

    video_target = destination.with_name(destination.name + _VIDEO_STREAM_SUFFIX)
    audio_target = destination.with_name(destination.name + _AUDIO_STREAM_SUFFIX)

    # The two halves can come from different CDN mirrors with different
    # rules -- on Bilibili every audio format sits on the mirror that wants
    # a Referer while some video formats do not -- so each carries its own.
    video_total = probe_total_bytes(chosen.url, client=client, headers=chosen.request_headers)
    audio_total = probe_total_bytes(audio.url, client=client, headers=audio.request_headers)
    if video_total is not None and audio_total is not None:
        pump.bytes_total = video_total + audio_total

    _fetch_stream(
        chosen.url,
        video_target,
        "video",
        video_total,
        client,
        pump,
        cancel,
        headers=chosen.request_headers,
    )
    _fetch_stream(
        audio.url,
        audio_target,
        "audio",
        audio_total,
        client,
        pump,
        cancel,
        headers=audio.request_headers,
    )

    pump.muxing()
    mux(video_target, audio_target, destination, ffmpeg=plan.ffmpeg)
    for intermediate in (video_target, audio_target):
        try:
            intermediate.unlink(missing_ok=True)
        except OSError:
            pass
    return destination.stat().st_size


# --- manifest ---------------------------------------------------------------


def _audio_language_lines(manifest: Manifest) -> list[str]:
    """Say which language the saved audio speaks, when there was a choice.

    The disclosure half of P-49. The selector now prefers the original
    track, but "the picker chose well" is not something the archive can
    show a year later -- and the failure it replaces was invisible for
    exactly that reason: an Arabic dub sat beside English subtitles with
    nothing on disk recording that a choice had been made at all.

    Silent when the source named no language, which is every platform
    without dubs. A line that appears on every download is a line nobody
    reads.
    """
    spoken: list[str] = []
    dubbed = False
    for item in manifest.items:
        track = item.audio
        if track is None or not track.language:
            continue
        if track.language not in spoken:
            spoken.append(track.language)
        if track.is_original is False:
            dubbed = True
    if not spoken:
        return []

    lines = [f"Audio language: {', '.join(spoken)}"]
    if dubbed:
        # Not an error: asking for a dub is legitimate. But it is the one
        # case where what you hear is not what was said, so it says so.
        lines.append("Audio track: a dub, not the video's original audio")
    return lines


def _write_sidecar_files(manifest: Manifest, plan: TransferPlan, post_dir: Path) -> None:
    """`manifest.json` and `_info.txt` beside the media.

    `manifest.json` is what makes `mfp fetch --manifest` (§4.1) work: it is
    how a re-fetch at a different quality happens without re-probing, which
    is the entire reason probe and fetch are separate verbs.
    """
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / manifest_filename()).write_text(
        manifest.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    source = manifest.source
    lines = [
        f"URL: {source.url}",
        f"Platform: {source.platform}",
        f"Author: {source.author or '-'}",
        f"Posted: {source.timestamp or '-'}",
        f"Items: {len(manifest.items)}",
    ]
    if manifest.degraded:
        lines.append(f"Degraded: {manifest.degraded_reason or 'yes'}")
    lines += _audio_language_lines(manifest)
    if source.caption:
        lines += ["", source.caption]
    (post_dir / info_filename()).write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_manifest(
    manifest: Manifest,
    *,
    output_root: str | Path,
    budget: FetchResultBudget,
    ffmpeg: str | None = None,
    allow_silent_video: bool = False,
    client: httpx.Client | None = None,
    select: Collection[int] | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: Callable[[], bool] | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    mux: Callable[..., None] = mux_streams,
) -> FetchResult:
    """Transfer every selected item of `manifest`, returning a FetchResult.

    Serial by choice. §5.6 permits parallel transfers, but nothing has
    measured a need for them, and one stream at a time is what makes
    `bytesPerSec` mean something a user can read.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    moment = clock()
    plan = TransferPlan(
        output_root=Path(output_root),
        platform=manifest.source.platform,
        author=manifest.source.author,
        date=_date_from_manifest(manifest, now=moment),
        post_id=manifest.source.id,
        ffmpeg=ffmpeg,
        allow_silent_video=allow_silent_video,
    )

    owned_client = client is None
    active = client or build_client()
    results: list[FetchResultItem] = []
    stop_reason: Literal["budget_exhausted", "blocked", "user_cancelled"] | None = None

    try:
        for item in manifest.items:
            if stop_reason is not None or (select is not None and item.index not in select):
                results.append(FetchResultItem(index=item.index, status="skipped"))
                continue
            if cancel is not None and cancel():
                stop_reason = "user_cancelled"
                results.append(FetchResultItem(index=item.index, status="skipped"))
                continue
            try:
                results.append(
                    download_item(
                        item,
                        plan,
                        client=active,
                        post_url=manifest.source.url,
                        now=clock(),
                        on_progress=on_progress,
                        cancel=cancel,
                        monotonic=monotonic,
                        mux=mux,
                    )
                )
            except TransferCancelled:
                stop_reason = "user_cancelled"
                results.append(FetchResultItem(index=item.index, status="skipped"))
    finally:
        if owned_client:
            active.close()

    if any(row.status == "ok" for row in results):
        first_ok = next(row for row in results if row.status == "ok" and row.path)
        _write_sidecar_files(manifest, plan, Path(str(first_ok.path)).parent)

    requested = [row for row in results if row.status != "skipped"]
    return FetchResult(
        ok=bool(requested) and all(row.status == "ok" for row in requested) and not stop_reason,
        output_root=str(plan.output_root),
        items=results,
        budget=budget,
        stop_reason=stop_reason,
    )


def plan_destinations(
    manifest: Manifest,
    *,
    output_root: str | Path,
    now: Callable[[], datetime] | None = None,
) -> dict[int, str]:
    """Where each chosen item would land, without moving a byte (`--dry-run`).

    Uses the same `_destination_for` the real transfer uses rather than
    re-deriving the path, because a dry run that agrees with a plan nobody
    will follow is worse than no dry run: the two would drift and the
    preview would stop predicting the thing it exists to preview.

    Items with no `chosen` variant are absent from the result. Path errors
    (`path_too_long`, `path_escape`) propagate -- surfacing them before the
    download rather than during it is the point.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    moment = clock()
    plan = TransferPlan(
        output_root=Path(output_root),
        platform=manifest.source.platform,
        author=manifest.source.author,
        date=_date_from_manifest(manifest, now=moment),
        post_id=manifest.source.id,
    )
    return {
        item.index: str(_destination_for(item, item.chosen, plan))
        for item in manifest.items
        if item.chosen is not None
    }


def selected_indices(spec: str | None, available: Iterable[int]) -> set[int] | None:
    """Parse a `--select` spec (`0,2,5-7`) against the indices that exist.

    Returns None for "everything", which is a different answer from an
    empty set ("you selected nothing") and must not collapse into it.
    """
    if spec is None or not spec.strip():
        return None
    chosen: set[int] = set()
    known = set(available)
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            low, _, high = piece.partition("-")
            if not (low.strip().isdigit() and high.strip().isdigit()):
                raise ValueError(f"unparseable range in --select: {piece!r}")
            chosen.update(range(int(low), int(high) + 1))
        elif piece.isdigit():
            chosen.add(int(piece))
        else:
            raise ValueError(f"unparseable index in --select: {piece!r}")
    unknown = sorted(chosen - known)
    if unknown:
        raise ValueError(f"--select names items that do not exist: {unknown}")
    return chosen


__all__ = [
    "CHUNK_BYTES",
    "DEFAULT_USER_AGENT",
    "ProgressCallback",
    "ProgressEvent",
    "TransferCancelled",
    "TransferPlan",
    "asset_key",
    "build_client",
    "check_not_expired",
    "download_item",
    "download_manifest",
    "mux_streams",
    "plan_destinations",
    "measured_size",
    "probe_total_bytes",
    "resume_offset",
    "selected_indices",
    "transfer_stream",
]
