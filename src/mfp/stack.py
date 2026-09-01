"""`mfp stack` -- a video plus its subtitles, as one stacked quote image.

The output is the shape a reader already recognises: the first moment keeps
the whole frame, and every line after it contributes only its subtitle band,
laid down the page. Reference behaviour and the measurements behind every
rule here are in `docs/concept-quotestack-2026-08-19.md` and the two spike
reports beside it.

Three facts from those spikes drive the whole design:

1. **No OCR, no OpenCV, no numpy.** The stack is made of original pixels, so
   nothing here has to READ the text -- only find where and when it is.
   `ffmpeg` and `Pillow` cover it.

2. **Cues come from one of two places and the rest of the pipeline does not
   care which.** A caption file gives exact times but no pixels (the text has
   to be burned on); burned-in subtitles give pixels but no times (they have
   to be detected). `Cue` is where the two meet.

3. **A band is measured, never computed.** Font metrics cannot predict where
   a renderer wrapped a line, and brightness statistics cannot tell a
   subtitle from a white sleeve. Both were tried and both failed on real
   samples; see `find_band_hint` for what the failures cost.

**What this module is NOT.** It was four subsystems until 2026-08-30, when
the architecture health check's split trigger fired: four importers, of
which only two stacked anything. `transcript` and `translate` were here for
a cue parser and a timestamp regex -- `translate` reaching across the
boundary for three private names. They are now:

    `mfp.cues`       parsing cues and timecodes; pure, runs nothing
    `mfp.captions`   finding and fetching a caption track (yt-dlp)
    `mfp.mediatool`  running ffmpeg/ffprobe/yt-dlp, and their two failures

What is left here is the frame engine: measure a band, cut strips, compose.
Adding something to this file that a caller could want WITHOUT stacking a
frame is how the last split became necessary.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from mfp.cues import Cue, group_lines, read_cue_lines
from mfp.errors import (
    MediaToolFailed,
    MfpError,
    NoSubtitlePixelsInBand,
    StackCancelled,
    StackError,
    UsageError,
)
from mfp.mediatool import missing_tool, resolved_command, run_tool, with_evidence

try:  # pragma: no cover - import guard, exercised only on a broken install
    from PIL import Image, ImageChops, ImageDraw, ImageStat
except ModuleNotFoundError as exc:  # pragma: no cover
    raise MfpError(
        "Pillow is required for `mfp stack`; reinstall the package"
    ) from exc


# ---------------------------------------------------------------------------
# Look and behaviour. One block, because a visual change should be a number
# change and not a hunt through the module. The adjustment table in the docs
# maps "what you want to change" onto these names.
# ---------------------------------------------------------------------------
DEFAULTS = {
    # Subtitle rendering, soft-cue path only. These are ASS units at
    # PlayResY=288 -- ffmpeg converts SRT through an ASS with that script
    # resolution, so they scale by frame_height/288, they are NOT pixels.
    "font_name": "Arial",
    "font_size": 26,
    "bold": 1,
    "outline": 2.5,
    "shadow": 0,
    "margin_v": 80,
    "margin_lr": 40,
    "lines_per_strip": 2,
    # Characters per strip when the cue source is a plain transcript, whose
    # blocks are paragraphs rather than display lines. Roughly two rendered
    # rows at the default font.
    "transcript_chars": 70,

    # Band padding, in real pixels, added around whatever was measured.
    "band_pad_top": 14,
    "band_pad_bottom": 10,

    # Crop each strip to its OWN text, instead of to the tallest strip's
    # band. A two-line cue in a band sized for three lines carries the
    # difference as dead space, which is what the first M0a stack looked
    # like. Set False for strips of one uniform height.
    "tight_strips": True,
    "min_strip_rows": 40,

    # Hard-cue detection.
    "detect_fps": 6.0,
    "detect_threshold": 6.0,
    "min_hold_s": 0.7,
    "settle_s": 0.15,
    "white_level": 235,
    "min_strip_white": 0.012,

    # "above_text" cuts the opening frame off where its subtitle begins and
    # lets that subtitle join the column as a normal strip. "full" keeps the
    # whole opening frame, which puts its line inside the picture and starts
    # the column at the second one.
    "head_mode": "above_text",

    # Scanning economy. Every ffmpeg pass here used to run over the WHOLE
    # file whatever window was asked for. Measured 2026-08-23 on a 2151 s 4K
    # AV1 talk: the region scan alone is ~12,900 frames and about ten minutes
    # with nothing printed, which the M2 acceptance run reported as a hang --
    # correctly, since a silent process and a stuck one look the same. The
    # window is now handed to ffmpeg; these three govern what gets said while
    # it works, and when a doomed scan is stopped before it starts.
    "progress_every_s": 20.0,
    #: Below this much video the pre-check costs more than the scan it would
    #: save, and a short clip fails fast on its own.
    "precheck_span_s": 120.0,
    "precheck_samples": 8,

    # Output.
    "max_strips": 24,
    "out_width": 1080,
    "jpeg_quality": 92,
}

#: Settings whose value is not just a number of the right type.
_CHOICES = {"head_mode": ("above_text", "full")}


def apply_settings(pairs: Iterable[str], base: dict | None = None) -> dict:
    """`["font_size=20"]` -> `{"font_size": 20}`, typed like the default.

    The block above is the whole adjustment surface of this feature, and
    until this existed the only way to reach any of it was to edit the
    module -- including `transcript_chars`, which the acceptance checklist
    told the reader to change. A knob nobody can turn is a knob that does
    not exist.
    """
    base = DEFAULTS if base is None else base
    out: dict = {}
    for raw in pairs:
        key, sep, value = raw.partition("=")
        key = key.strip()
        if not sep:
            raise UsageError(f"--set wants KEY=VALUE; got {raw!r}")
        if key not in base:
            raise UsageError(
                f"unknown setting {key!r}. The settings are: "
                + ", ".join(f"{k}={base[k]!r}" for k in sorted(base))
            )
        current = base[key]
        try:
            if key in _CHOICES:
                if value not in _CHOICES[key]:
                    raise ValueError
                out[key] = value
            elif isinstance(current, bool):
                out[key] = {"true": True, "false": False}[value.strip().lower()]
            elif isinstance(current, int):
                out[key] = int(value)
            elif isinstance(current, float):
                out[key] = float(value)
            else:
                out[key] = value.strip()
        except (ValueError, KeyError):
            wanted = (
                " or ".join(_CHOICES[key]) if key in _CHOICES
                else type(current).__name__
            )
            raise UsageError(
                f"--set {key} wants {wanted}; got {value!r}"
            ) from None
    return out

@dataclass
class StackResult:
    output: Path
    preview: Path | None
    strips: int
    cues_found: int
    dropped_blank: int
    band: tuple[int, int]
    source: str
    truncated: int = 0
    width: int = 0
    height: int = 0

    def to_payload(self) -> dict:
        return {
            "schemaVersion": 1,
            "output": str(self.output),
            "preview": str(self.preview) if self.preview else None,
            "source": self.source,
            "strips": self.strips,
            "cuesFound": self.cues_found,
            "droppedBlank": self.dropped_blank,
            "truncated": self.truncated,
            "band": {"top": self.band[0], "bottom": self.band[1]},
            "size": {"width": self.width, "height": self.height},
        }


def _run_progress(cmd: list[str], span: float, label: str, say, every: float,
                  should_cancel=None) -> None:
    """Run one ffmpeg pass, saying how far through the window it has got.

    `-progress pipe:1` is ffmpeg's own machine-readable tick, so this reports
    what ffmpeg has actually finished rather than a guess from elapsed time.
    stderr is folded into the same pipe on purpose: reading one stream while
    the other fills its buffer is how a subprocess deadlocks, and the only
    thing stderr carries under `-v error` is the failure tail.

    `should_cancel` is checked on each tick rather than between passes: this
    IS the long pass, and a stop button that only takes effect when ffmpeg
    finishes is a stop button that does nothing on the runs that need one.
    """
    started = time.monotonic()
    # The same resolution `run_tool` does, because this is the pass that
    # takes minutes and it is not allowed to be the one call that finds a
    # different ffmpeg -- or that reports a missing one as a traceback.
    cmd = resolved_command(cmd)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        logs.ran(cmd[0], args=cmd[1:], code=None, ms=0)
        raise missing_tool(cmd[0]) from exc
    tail: list[str] = []
    noise: list[str] = []
    last = -every
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        key, sep, value = line.partition("=")
        if not sep:
            if line:
                tail = (tail + [line])[-3:]
                # The bundle wants the whole conversation, bounded so a
                # pathological run cannot eat memory.
                if len(noise) < 500:
                    noise.append(line)
            continue
        if key != "out_time_us" or not value.isdigit():
            continue
        if should_cancel is not None and should_cancel():
            proc.terminate()
            proc.wait(timeout=5)
            raise StackCancelled()
        done = int(value) / 1_000_000
        if span > 0 and done - last >= every:
            last = done
            say(f"{label}: {done:.0f}s / {span:.0f}s")
    proc.wait()
    logs.ran(cmd[0], args=cmd[1:], code=proc.returncode,
             ms=int((time.monotonic() - started) * 1000))
    if proc.returncode != 0:
        raise with_evidence(
            MediaToolFailed(f"ffmpeg failed: {' / '.join(tail)}"),
            command=cmd, stderr="\n".join(noise),
        )


def probe_video(path: Path) -> tuple[int, int, float]:
    """(width, height, duration). Raises if the file carries no video stream.

    Worth being strict about: a fetch can hand back an audio-only rendition
    for a video post, and every later step would then work on nothing. A
    clear failure here beats an empty image later.
    """
    out = run_tool([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration", "-of", "json", str(path),
    ])
    data = json.loads(out or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise StackError(
            f"{path.name} has no video stream -- nothing to take frames from"
        )
    w = int(streams[0].get("width") or 0)
    h = int(streams[0].get("height") or 0)
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    if not (w and h):
        raise StackError(f"{path.name} reports no frame size")
    return w, h, dur


def _escape_filter_path(p: Path) -> str:
    """Windows paths inside an ffmpeg filter argument.

    `subtitles=` parses its own value, so the drive colon has to survive a
    second round of parsing and backslashes have to stop being escapes.
    """
    return str(p).replace("\\", "/").replace(":", "\\:")


def _force_style(opts: dict) -> str:
    return (
        f"FontName={opts['font_name']},FontSize={opts['font_size']},"
        f"Bold={opts['bold']},Outline={opts['outline']},Shadow={opts['shadow']},"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Alignment=2,"
        f"MarginV={opts['margin_v']},MarginL={opts['margin_lr']},"
        f"MarginR={opts['margin_lr']}"
    )


#: One cue held for ten hours. The frame this is rendered onto is reached by
#: an input seek, and whether that leaves the frame's timestamp at zero or at
#: its position in the source depends on the container -- so the cue is given
#: a span no timestamp can fall outside, and the question stops mattering.
_ALWAYS = "00:00:00,000 --> 10:00:00,000"


def burn_frame(video: Path, t: float, text: str, dest: Path, opts: dict) -> Path:
    """One frame of `video`, with one cue rendered onto it by libass.

    The soft path used to burn the ENTIRE clip to an intermediate mp4 and
    then seek into it. That cost an x264 encode of every frame between the
    first cue and the last -- minutes on a long 4K source, and a file bigger
    than the download -- to read at most `max_strips` frames back out of it.
    Rendering each wanted frame on its own is the same libass call at the
    same frame size, so the pixels are the ones the old path produced, minus
    a lossy generation: an encode the measurement in `measure_boxes` was
    having to see through.

    It also removes an accident: in a whole-clip burn, a frame grabbed near a
    cue boundary could carry the neighbouring cue as well.
    """
    srt = dest.with_suffix(".srt")
    srt.write_text(f"1\n{_ALWAYS}\n{text}\n", encoding="utf-8")
    run_tool([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}",
        "-i", str(video), "-frames:v", "1",
        "-vf", f"subtitles='{_escape_filter_path(srt)}':"
               f"force_style='{_force_style(opts)}'",
        str(dest),
    ])
    return dest


def grab_frame(video: Path, t: float, dest: Path, *,
               max_width: int | None = None) -> Path:
    """One frame at `t`. `max_width` scales it down on the way out.

    Full size for everything this module measures -- the band is in real
    pixels and a resized frame would move it -- and scaled only for the
    region picker, which is showing a person where the subtitles are, not
    measuring them.
    """
    scale = ["-vf", f"scale={max_width}:-2"] if max_width else []
    run_tool(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}",
          "-i", str(video), "-frames:v", "1", *scale, str(dest)])
    return dest


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------
def measure_boxes(burned: Iterable[Path], clean: Iterable[Path],
                  threshold: int = 40) -> list[tuple[int, int] | None]:
    """Per-frame vertical extent of the pixels the renderer wrote.

    Computing a band from FontSize and MarginV clipped the top row of every
    wrapped cue, because font metrics cannot know where libass broke the
    line. Differencing each burned frame against the same timestamp from the
    unburned clip marks exactly the written pixels instead. Only possible on
    the soft path -- it is the unburned frame that makes it possible.

    Kept per frame rather than unioned so each strip can be cropped to its
    own text; the union is still available by taking the extremes.
    """
    boxes: list[tuple[int, int] | None] = []
    for b, c in zip(burned, clean):
        diff = ImageChops.difference(
            Image.open(b).convert("L"), Image.open(c).convert("L")
        ).point(lambda v: 255 if v > threshold else 0)
        box = diff.getbbox()
        boxes.append((box[1], box[3]) if box else None)
    return boxes


def tight_rows(frame: Path, band: tuple[int, int], width: int,
               white_level: int, run_cover: int = 8) -> tuple[int, int] | None:
    """Where the text actually sits inside an already-known band.

    The hard path has no clean reference frame to difference against, so it
    finds the text the way the diagnostic that cracked M0b did: rows whose
    near-white coverage clears a floor. Confined to a band that is already
    known to hold subtitles, which is what makes it safe here and unsafe as
    a way of FINDING the band (see `find_band_hint`).
    """
    crop = Image.open(frame).convert("L").crop((0, band[0], width, band[1]))
    mask = crop.point(lambda v: 255 if v >= white_level else 0)
    profile = list(mask.resize((1, mask.height), Image.BOX).getdata())
    rows = [i for i, v in enumerate(profile) if v >= run_cover]
    if not rows:
        return None
    return band[0] + rows[0], band[0] + rows[-1] + 1


def pad_band(band: tuple[int, int], height: int, opts: dict) -> tuple[int, int]:
    top = max(0, band[0] - opts["band_pad_top"])
    bottom = min(height, band[1] + opts["band_pad_bottom"])
    if bottom - top < opts["min_strip_rows"]:
        bottom = min(height, top + opts["min_strip_rows"])
    return top, bottom


def find_band_hint(video: Path, height: int, opts: dict) -> None:
    """Deliberately not implemented -- automatic band detection is unsolved.

    Three rules were measured against a real Reel whose subtitles sit at
    y=1316-1527 on a 1920 frame, and all three missed:

      * burstiness of frame difference   -> y 732-1684  (35-43% of frame)
      * burstiness of near-white coverage -> y 1224-1364 (top line only)
      * occupancy of text-like row runs   -> y 860-1312

    The measurements say why. Rows over a desk scored HIGHER on both
    burstiness and near-white coverage than the real subtitle rows, so
    brightness statistics cannot separate them; and that clip's subtitle
    block moves depending on how many lines a cue has, so the rows are
    neither consistently bright nor mutually synchronised.

    `--roi` is therefore a required argument on the hard path, not a power
    user's override, and `--preview` exists so the choice can be checked
    before a long image is built on it.
    """
    raise UsageError(
        "--roi TOP:BOTTOM is required for burned-in subtitles "
        "(automatic band detection is not reliable; use --preview to check a guess)"
    )


def parse_roi(raw: str, height: int) -> tuple[int, int]:
    parts = raw.split(":")
    if len(parts) != 2:
        raise UsageError("--roi must look like TOP:BOTTOM, e.g. 1300:1545")
    try:
        top, bottom = int(parts[0]), int(parts[1])
    except ValueError:
        raise UsageError("--roi bounds must be whole pixels") from None
    if not (0 <= top < bottom <= height):
        raise UsageError(
            f"--roi {top}:{bottom} is outside the frame (height {height})"
        )
    return top, bottom


# ---------------------------------------------------------------------------
# Hard cues -- detect when the burned-in line changes
# ---------------------------------------------------------------------------
def segment_stable(diffs: Sequence[float], fps: float, threshold: float,
                   min_hold: float) -> list[tuple[float, float]]:
    """Turn a per-frame difference series into the intervals that held still.

    A run of consecutive high-difference frames is ONE transition, not
    several: a subtitle swap takes a few frames, and counting each of them
    would split one line across several strips.
    """
    bounds: list[int] = []
    last = -99
    for i, d in enumerate(diffs):
        if d >= threshold:
            if i - last > 1:
                bounds.append(i)
            last = i
    step = 1.0 / fps
    edges = [0] + bounds + [len(diffs)]
    out: list[tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        if (b - a) * step >= min_hold:
            out.append((a * step, b * step))
    return out


def window_args(start: float, end: float | None) -> list[str]:
    """The input-side seek and duration for one window, ready for ffmpeg.

    Input-side (`-ss` before `-i`) on purpose: an output-side seek decodes
    everything ahead of the window and throws it away, which is the cost this
    exists to avoid. Kept as its own function because "did the window reach
    ffmpeg at all" is the defect being fixed here, and a function can be
    tested without decoding anything.
    """
    args: list[str] = []
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    if end is not None and end > start:
        args += ["-t", f"{end - start:.3f}"]
    return args


def roi_difference_series(video: Path, band: tuple[int, int], width: int,
                          workdir: Path, opts: dict, *, start: float = 0.0,
                          end: float | None = None, say=None,
                          should_cancel=None) -> list[float]:
    """Per-sample change inside the band, over the requested window only.

    The window is the whole point of the signature: this used to take none
    and scan the entire file, so `--from`/`--to` only ever filtered the
    RESULT of work already done in full.
    """
    say = say or (lambda _m: None)
    top, bottom = band
    frames = workdir / "roi"
    frames.mkdir(parents=True, exist_ok=True)
    for stale in frames.glob("*.jpg"):
        stale.unlink()

    span = (end - start) if end is not None else 0.0
    expected = int(span * opts["detect_fps"]) if span > 0 else 0
    say(f"scanning rows {top}-{bottom} over {span:.0f}s at "
        f"{opts['detect_fps']:g} fps ({expected} samples)")
    _run_progress(
        ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
        + window_args(start, end)
        + ["-i", str(video),
           "-vf", (f"crop={width}:{bottom - top}:0:{top},"
                   f"fps={opts['detect_fps']},scale=-2:120"),
           "-q:v", "3", str(frames / "r%05d.jpg")],
        span, "  scanning", say, opts["progress_every_s"], should_cancel,
    )

    series: list[float] = [0.0]
    prev = None
    for f in sorted(frames.glob("*.jpg")):
        img = Image.open(f).convert("L")
        if prev is not None:
            series.append(ImageStat.Stat(ImageChops.difference(img, prev)).mean[0])
        prev = img
    if len(series) < 2:
        raise StackError("no frames decoded from the region of interest")
    # The samples have said everything they had to say, and on a long window
    # there are thousands of them. Left behind once as 5,781 files in a
    # user's output folder, which is how the leak was noticed. A failed scan
    # keeps them: then they are evidence.
    for spent in frames.glob("*.jpg"):
        spent.unlink()
    return series


def sample_band_fractions(video: Path, band: tuple[int, int], width: int,
                          workdir: Path, opts: dict, start: float,
                          end: float) -> list[tuple[Path, float]]:
    """How subtitle-white the band is at a few points across the window.

    Cheap enough to run before a scan that is not: each sample is one seek.
    What it can determine is narrow -- "these particular frames hold no
    subtitle-bright pixels in these rows" -- and the caller is careful to
    claim no more than that.
    """
    probe = workdir / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    n = max(2, int(opts["precheck_samples"]))
    step = (end - start) / n
    out: list[tuple[Path, float]] = []
    for i in range(n):
        f = grab_frame(video, start + step * (i + 0.5), probe / f"p{i:02d}.png")
        out.append((f, band_white_fraction(f, band, width, opts["white_level"])))
    return out


def band_white_fraction(frame: Path, band: tuple[int, int], width: int,
                        white_level: int) -> float:
    """How much of the band is subtitle-white. Near zero means no line."""
    crop = Image.open(frame).convert("L").crop((0, band[0], width, band[1]))
    return ImageStat.Stat(
        crop.point(lambda v: 1 if v >= white_level else 0)
    ).mean[0]


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def compose(frames: Sequence[Path], bands: Sequence[tuple[int, int]],
            width: int, opts: dict) -> Image.Image:
    """First frame whole, every later one cropped to its band, stacked.

    `bands` is per frame rather than one shared band: strips cropped to the
    tallest cue's height carry the difference as dead space above the
    shorter ones.
    """
    if not frames:
        raise StackError("nothing to compose")
    if len(bands) != len(frames):
        raise StackError("one band per frame is required")
    tiles: list[Image.Image] = []
    for i, (p, band) in enumerate(zip(frames, bands)):
        im = Image.open(p).convert("RGB")
        if i > 0:
            tiles.append(im.crop((0, band[0], width, band[1])))
            continue
        if opts["head_mode"] == "full":
            tiles.append(im)
            continue
        # The head stops where the first line starts, and that line is then
        # laid down as an ordinary strip. The two crops are adjacent slices
        # of the same frame, so they still read as one picture -- but every
        # line now appears in exactly one place, in the same treatment as
        # the others. A whole head frame puts its own line inside the
        # picture and starts the column at the second one, which reads as
        # the first line having been said twice.
        if band[0] > 0:
            tiles.append(im.crop((0, 0, width, band[0])))
        tiles.append(im.crop((0, band[0], width, band[1])))
    total = sum(t.height for t in tiles)
    canvas = Image.new("RGB", (width, total), (0, 0, 0))
    y = 0
    for t in tiles:
        canvas.paste(t, (0, y))
        y += t.height
    out_w = opts["out_width"]
    if out_w and out_w != width:
        canvas = canvas.resize(
            (out_w, max(1, round(total * out_w / width))), Image.LANCZOS
        )
    return canvas


def write_preview(frame: Path, band: tuple[int, int], width: int,
                  dest: Path) -> Path:
    """The band drawn on a real frame, so a crop can be judged before a
    2000-pixel-tall image is built on it."""
    img = Image.open(frame).convert("RGB")
    ImageDraw.Draw(img).rectangle(
        [2, band[0], width - 3, band[1]], outline=(255, 0, 0), width=5
    )
    img.save(dest, quality=90)
    return dest


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_stack(
    *,
    video: Path,
    out: Path,
    workdir: Path,
    subs: Path | None = None,
    roi: str | None = None,
    start: float = 0.0,
    end: float | None = None,
    offset: float = 0.0,
    preview: bool = False,
    opts: dict | None = None,
    on_progress=None,
    should_cancel=None,
) -> StackResult:
    """`video (+ cues) -> one stacked image`.

    `offset` is the video file's t=0 expressed in the caption file's
    timeline, for when the clip was cut out of a longer source. It has no
    meaning on the hard path, where the only timeline is the file's own.
    """
    o = {**DEFAULTS, **(opts or {})}
    say = on_progress or (lambda _m: None)
    stop = should_cancel or (lambda: False)

    def checkpoint() -> None:
        """Between two pieces of work, ask whether there should be a third.

        Frame grabs are seconds apart on a 4K source, so checking here is
        what makes a stop button feel like one; the long scan checks on its
        own ticks.
        """
        if stop():
            raise StackCancelled()

    workdir.mkdir(parents=True, exist_ok=True)
    shots = workdir / "shots"
    shots.mkdir(exist_ok=True)
    # Made here rather than beside `canvas.save`: a refusal can now write a
    # preview of the band it refused on, and that is worth more than the
    # image it replaces.
    out.parent.mkdir(parents=True, exist_ok=True)

    width, height, duration = probe_video(video)
    w0 = start
    w1 = end if end is not None else (duration + offset if subs else duration)
    if w1 <= w0:
        if end is None:
            # No `--to` was given, so the window ends at the file's own end --
            # and reporting that as "--to must be after --from" names an
            # argument the caller never typed, which is how the real fault
            # (a --from past the end) reads as a mistake in the message.
            raise UsageError(
                f"--from {w0:g}s is at or past the end of this "
                f"{duration:.1f}s video"
            )
        raise UsageError(f"--to ({w1:g}) must be after --from ({w0:g})")
    say(f"{video.name}: {width}x{height}, {duration:.1f}s")

    dropped = 0
    truncated = 0

    if subs is not None:
        lines, is_transcript = read_cue_lines(
            subs.read_text(encoding="utf-8-sig"), w0, w1, o["transcript_chars"]
        )
        kind = "transcript blocks" if is_transcript else "distinct caption lines"
        say(f"{len(lines)} {kind} in window")
        # A transcript block is already a strip's worth of speech; grouping
        # would put two paragraphs on one strip.
        cues = group_lines(lines, 1 if is_transcript else o["lines_per_strip"])
        found = len(cues)
        if not cues:
            raise StackError("the caption file has no lines in that window")
        if not any((c.end - offset) > 0 and (c.start - offset) < duration
                   for c in cues):
            # Every cue lands off the end of the clip. Almost always a wrong
            # --offset, and saying so beats letting the burn produce a video
            # with no text on it and reporting "no subtitle pixels found",
            # which describes the symptom and not the cause.
            raise StackError(
                f"with --offset {offset:g}s every caption falls outside this "
                f"{duration:.1f}s video; check --offset against the caption "
                f"timeline"
            )
        if len(cues) > o["max_strips"]:
            truncated = len(cues) - o["max_strips"]
            cues = cues[: o["max_strips"]]
        say(f"{len(cues)} strips; rendering each cue onto its own frame")

        picked: list[Path] = []
        clean: list[Path] = []
        for i, c in enumerate(cues):
            checkpoint()
            t = max(0.05, min(c.end - offset - 0.35, c.end - offset))
            picked.append(burn_frame(video, t, c.text, shots / f"b{i:03d}.png", o))
            clean.append(grab_frame(video, t, shots / f"c{i:03d}.png"))
            if (i + 1) % 8 == 0 and i + 1 < len(cues):
                say(f"  {i + 1}/{len(cues)} frames")
        boxes = measure_boxes(picked, clean)
        if not any(boxes):
            raise StackError("no rendered subtitle pixels found in any frame")
        union = (min(b[0] for b in boxes if b), max(b[1] for b in boxes if b))
        band = pad_band(union, height, o)
        if o["tight_strips"]:
            bands = [pad_band(b, height, o) if b else band for b in boxes]
        else:
            bands = [band] * len(picked)
        frames = picked
        source = "soft"
    else:
        if roi is None:
            find_band_hint(video, height, o)  # always raises, with the reason
        band = parse_roi(roi, height)
        # The window in the file's own timeline. `offset` has no meaning on
        # this path -- the pixels ARE the timeline -- so w0/w1 are already it.
        scan_start = max(0.0, min(w0, duration))
        scan_end = min(w1, duration)
        if scan_end - scan_start <= 0:
            # Reachable only with an explicit `--to`: `--from 5:00 --to 6:00`
            # on a 68 s clip clamps to an empty window. Without this it costs
            # a decode to find out, and reports "no frames" for what is
            # really "you asked past the end".
            raise UsageError(
                f"--from {w0:g}s is at or past the end of this "
                f"{duration:.1f}s video"
            )
        if scan_end - scan_start > o["precheck_span_s"]:
            samples = sample_band_fractions(
                video, band, width, workdir, o, scan_start, scan_end
            )
            best_frame, best = max(samples, key=lambda s: s[1])
            say(f"pre-check: brightest of {len(samples)} sampled frames carries "
                f"{best:.3f} subtitle-white in the band")
            if best >= o["min_strip_white"]:
                # Passed: the samples have nothing left to say. They are kept
                # only on the refusal path, where they are the evidence.
                for spent in samples:
                    spent[0].unlink(missing_ok=True)
            if best < o["min_strip_white"]:
                shown = None
                if preview:
                    shown = write_preview(
                        best_frame, band, width,
                        out.with_name(out.stem + "-preview.jpg"),
                    )
                raise with_evidence(NoSubtitlePixelsInBand(
                    f"no subtitle-bright pixels in rows {band[0]}-{band[1]} in any of "
                    f"{len(samples)} frames sampled across "
                    f"{scan_start:.0f}-{scan_end:.0f}s (brightest {best:.4f}, floor "
                    f"{o['min_strip_white']}). That band is "
                    f"{band[0] / height:.0%}-{band[1] / height:.0%} down a {height}-row "
                    f"frame. Either --roi is aimed at the wrong rows -- run --preview "
                    f"and look -- or this video's subtitles are a separate caption "
                    f"track rather than burned into the picture, in which case pass "
                    f"--subs (a caption file, or the post URL to fetch one from) "
                    f"instead of --roi"
                ),
                    # The frames it judged on, so the refusal can be checked
                    # rather than taken on trust.
                    command=["mfp", "stack", str(video), "--roi", roi or ""],
                    stderr=None,
                    evidence=[p for p, _ in samples] + ([shown] if shown else []),
                )
        diffs = roi_difference_series(
            video, band, width, workdir, o,
            start=scan_start, end=scan_end, say=say, should_cancel=stop,
        )
        segments = segment_stable(
            diffs, o["detect_fps"], o["detect_threshold"], o["min_hold_s"]
        )
        # `segment_stable` counts from the first sample it was given, and the
        # first sample is now the start of the window rather than of the file.
        segments = [(a + scan_start, b + scan_start) for a, b in segments]
        segments = [s for s in segments if s[1] > w0 and s[0] < w1]
        say(f"{len(segments)} stable segments in window")
        found = len(segments)

        frames = []
        for i, (a, b) in enumerate(segments):
            checkpoint()
            t = min(a + o["settle_s"], max(a, b - 0.1))
            f = grab_frame(video, t, shots / f"s{i:03d}.png")
            # A change is also fired when a line LEAVES. Those gaps carry no
            # text, and without this they become blank strips in the stack.
            if band_white_fraction(f, band, width, o["white_level"]) < o["min_strip_white"]:
                dropped += 1
                continue
            frames.append(f)
            if len(frames) % 8 == 0:
                say(f"  {len(frames)} strips so far")
        if not frames:
            raise StackError(
                "no subtitle-bearing frames found in that region -- "
                "check --roi with --preview"
            )
        if len(frames) > o["max_strips"]:
            truncated = len(frames) - o["max_strips"]
            frames = frames[: o["max_strips"]]
        say(f"{len(frames)} strips with text, {dropped} blank gaps dropped")
        if o["tight_strips"]:
            bands = [
                pad_band(t, height, o) if t else band
                for t in (tight_rows(f, band, width, o["white_level"])
                          for f in frames)
            ]
        else:
            bands = [band] * len(frames)
        source = "hard"

    canvas = compose(frames, bands, width, o)
    canvas.save(out, quality=o["jpeg_quality"])

    preview_path = None
    if preview:
        preview_path = write_preview(
            frames[0], band, width, out.with_name(out.stem + "-preview.jpg")
        )

    if truncated:
        say(f"NOTE: {truncated} further strips were dropped by max_strips="
            f"{o['max_strips']}")

    return StackResult(
        output=out,
        preview=preview_path,
        strips=len(frames),
        cues_found=found,
        dropped_blank=dropped,
        band=band,
        source=source,
        truncated=truncated,
        width=canvas.size[0],
        height=canvas.size[1],
    )
