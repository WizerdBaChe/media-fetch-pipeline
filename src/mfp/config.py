"""Application configuration: load/save `%APPDATA%/media-fetch-pipeline/config.json`
(PSM Batch 1 core §4.6).

Fail-safe rules (PSM §13 Rollback):
  - The config file is versioned with `schemaVersion`.
  - If the file is missing, unreadable, not valid JSON, or fails schema
    validation (including an unknown `schemaVersion`), it is rebuilt from
    defaults rather than crashing the caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from mfp.models import CamelModel

_APP_DIR_NAME = "media-fetch-pipeline"
_CONFIG_FILE_NAME = "config.json"


class ChromeConfig(CamelModel):
    executable_path: str | None = None
    profile_dir: str | None = None
    window_position: tuple[int, int] = (-32000, -32000)
    visible: bool = False


class BudgetPlatformConfig(CamelModel):
    max_requests_per_hour: int
    max_requests_per_run: int
    min_interval_ms: int
    jitter_ms: int
    cooldown_on_block_ms: int


class BudgetConfig(CamelModel):
    instagram: BudgetPlatformConfig = Field(
        default_factory=lambda: BudgetPlatformConfig(
            max_requests_per_hour=120,
            max_requests_per_run=40,
            min_interval_ms=4000,
            jitter_ms=2500,
            cooldown_on_block_ms=1_800_000,
        )
    )
    #: Bilibili's own bucket, added 2026-09-03 (D-155). PSM §4.6 named two
    #: buckets, so Bilibili sat in `default` and was polled at 1s intervals
    #: -- four times faster than Instagram -- against a risk-control system
    #: that answers HTTP 412 on frequency. The pacing is what moved; the
    #: cooldown is left at the default because nothing here has measured how
    #: long Bilibili's window actually is, and inventing a number would be
    #: the tuning this project keeps deciding not to do on one data point.
    bilibili: BudgetPlatformConfig = Field(
        default_factory=lambda: BudgetPlatformConfig(
            max_requests_per_hour=120,
            max_requests_per_run=40,
            min_interval_ms=2500,
            jitter_ms=1500,
            cooldown_on_block_ms=300_000,
        )
    )
    default: BudgetPlatformConfig = Field(
        default_factory=lambda: BudgetPlatformConfig(
            max_requests_per_hour=300,
            max_requests_per_run=100,
            min_interval_ms=1000,
            jitter_ms=500,
            cooldown_on_block_ms=300_000,
        )
    )

    def for_platform(self, platform: str) -> BudgetPlatformConfig:
        """Look up the budget config for `platform`, falling back to the
        `default` bucket for any platform this class has no field for.

        Reads the field by NAME rather than testing platforms one at a time:
        the two-branch version was written when PSM §4.6 named exactly two
        buckets, and adding a third meant remembering to extend a lookup in
        a different file from the one the bucket is declared in. Declaring
        the field is now the whole of adding a bucket.

        The declared-fields check comes FIRST and is read off the class, not
        the instance. A bare `getattr(self, platform)` would reach every
        attribute this class has, including Pydantic's own -- and asking an
        instance for `model_fields` emits a deprecation warning, so the
        permissive version was one platform name away from printing a
        warning in production for a lookup that was going to fall through to
        `default` anyway.
        """
        if platform in type(self).model_fields:
            bucket = getattr(self, platform)
            if isinstance(bucket, BudgetPlatformConfig):
                return bucket
        return self.default


class BinariesConfig(CamelModel):
    yt_dlp: str | None = None
    gallery_dl: str | None = None
    ffmpeg: str | None = None


class GuidesConfig(CamelModel):
    """Which one-time explanations this person has already been shown.

    In the config rather than in the browser's storage, and that is the
    whole point: a guide is shown ONCE per person, and the renderer's
    storage is per Electron profile, per user-data directory, and gone the
    moment anything resets it. Somebody who is told how 逐字稿 works on
    every launch learns to dismiss it without reading, which is worse than
    never having shown it.

    Ids are opaque strings owned by the GUI. Deliberately not an enum here:
    the server has no opinion about which explanations exist, and a schema
    that had one would need a migration every time a tool gains a page.
    """

    seen: list[str] = Field(default_factory=list)


class AsrConfig(CamelModel):
    """Speech recognition, which lives OUTSIDE this process (see `mfp.asr`).

    Every field here describes a tool this product does not ship. That is
    the point: the engine is ~400 MB of Python and a ~3 GB model against a
    ~107 MB installer, so it is discovered the way yt-dlp and ffmpeg are
    rather than bundled. An empty `python` is the normal state on a machine
    that has never transcribed audio, and `doctor` says so plainly.
    """

    #: The interpreter that can `import faster_whisper`. NOT this process's
    #: own: in a packaged build `sys.executable` is `mfp.exe`, which cannot
    #: import anything.
    python: str | None = None
    #: The MODEL HOME: one directory, holding one folder per model.
    #:
    #: `None` no longer means "let the engine decide". It means "use the
    #: default for how this copy was installed" -- beside the program for an
    #: installed build, under the output root for a portable one (user
    #: ruling 2026-08-28, `asr_models.default_model_home`). The old
    #: behaviour handed the question to faster-whisper's Hugging Face cache,
    #: which put 3 GB somewhere nobody had named and gave the settings panel
    #: nothing it could show.
    model_dir: str | None = None
    #: Which model in that home to use. A NAME, matched leniently against
    #: the folders that are actually there (`large-v3` finds
    #: `faster-whisper-large-v3`), and used as a download instruction only
    #: when no local folder answers to it.
    model: str = "large-v3"
    #: `auto` prefers CUDA and falls back to CPU. Transcribing slowly is a
    #: different outcome from not transcribing.
    device: Literal["auto", "cuda", "cpu"] = "auto"
    #: `auto` means float16 on CUDA, int8 on CPU. int8 on CUDA is NOT the
    #: default even though it is faster: it fails with
    #: CUBLAS_STATUS_NOT_SUPPORTED on RTX 50-series (sm_120).
    compute_type: str = "auto"
    #: `trad` asks Whisper for Traditional Chinese when the audio is
    #: Chinese; it writes Simplified otherwise. Measured 2026-08-27, not
    #: assumed -- the lever is a prompt, and a prompt is a hint.
    script: Literal["trad", "none"] = "trad"
    #: What to do to the sound BEFORE recognising it.
    #:
    #: `none` is the default and the recommendation, and that is a measured
    #: position rather than a conservative one: on the recording that made
    #: this setting exist, doing nothing and letting the retry ladder turn
    #: the voice-activity filter off produced 158 segments, while evening out
    #: the levels produced 125 and cost a whole extra stage (D-113).
    #:
    #: `level` is for a lopsided recording -- a call where one side is much
    #: quieter -- and `denoise` is for a loud speaker in a noisy room. The
    #: second one is measurably useless on the first one's audio, which the
    #: GUI says out loud rather than leaving the reader to find out.
    audio: Literal["none", "level", "denoise"] = "none"
    #: Which model in the same home translates a finished transcript.
    #:
    #: A SEPARATE field from `model`, not a mode of it, because the two are
    #: different kinds of model and the two capabilities are independently
    #: available: a machine can transcribe and not translate, or the other
    #: way round, and one setting could not say so. `None` is the normal
    #: state -- translation is opt-in and nothing downloads it.
    translation_model: str | None = None
    #: Default target language for translation, FLORES-200 style. Named
    #: rather than detected: what to translate INTO is a decision, and a
    #: tool that guessed it would be guessing at the one thing the user
    #: opened the feature to say.
    translation_target: str = "zho_Hant"
    #: Permit the engine to fetch model weights it does not have. Off by
    #: default because a silent 3 GB download is indistinguishable from a
    #: hang.
    allow_download: bool = False


class ServeConfig(CamelModel):
    """`mfp serve` binding (PSM Batch 2 §4.1).

    `host` is not a free-form setting: O-3 rules non-loopback binding
    prohibited, and `mfp serve` refuses to start on anything else. It is a
    field rather than a constant only so tests can bind `::1`.
    """

    host: str = "127.0.0.1"
    port: int = 47821


class BriefConfig(CamelModel):
    """`mfp brief` defaults (post-brief PSM §5, B-10).

    Separate from the global `policy` on purpose. That one also governs
    video, where "720" means a 720p rendition; here it names a rung on
    Instagram's aspect-preserving image ladder, where 720 measures 720x900
    on a 4:5 photo.
    """

    #: The balanced rung one fetch aims for -- the file is BOTH what the
    #: agent looks at and what is kept (D-89), so this is the only quality
    #: decision the post ever gets.
    #:
    #: `max-height:720` selects the `p720x720` rendition, measured at
    #: 720x900 and 88,728 bytes. NOT `max-height:750`: that lands on
    #: `s750x750`, which is a SQUARE CROP of a 4:5 photo (D-92) and would
    #: archive every portrait picture with its top and bottom cut off,
    #: permanently, under a ruling to keep exactly one file per image.
    policy: str = "max-height:720"
    #: Which lane an agent that did not say gets. `content` explains the
    #: post; `visual` inventories layout and style.
    lane_default: Literal["content", "visual"] = "content"


class AppConfig(CamelModel):
    schema_version: Literal[1] = 1
    # O-2 ruled 2026-08-12 (PSM Batch 2 §9).
    output_root: str = "D:/output/MediaGrabbed"
    policy: str = "best"
    # --- §9's settings table ------------------------------------------------
    #
    # Only the rows something actually READS live here. 下載完成後 and
    # 重複貼上時 are deliberately absent until their consumers exist: a
    # stored setting nothing reads is a switch that does nothing, which is
    # the defect this milestone is meant to remove rather than add.
    #
    # 同時下載數 will never be here. §9 lists it as 1-8, but that table was
    # written before D-36 (2026-08-17) ruled transfers serial; the Settings
    # panel shows it the way §9 itself handles 速率上限 -- visible,
    # explained, not raisable.
    #
    #: O-6: drop COMPLETED rows older than this many days. `0` disables it.
    #: **Records only, never files** -- INV-6 says removing a record never
    #: deletes what was downloaded, and an automatic sweep is exactly where
    #: that promise would be easiest to break.
    auto_clear_days: int = Field(default=3, ge=0, le=365)
    #: How long a day's run log is kept. `0` disables the sweep, the same
    #: convention `auto_clear_days` uses, so the settings panel has one
    #: meaning for "off" rather than two.
    #:
    #: Error bundles are deliberately NOT covered by this: they are kept
    #: until somebody deletes them, because the failure a bundle describes
    #: is usually noticed long after the day it happened (user ruling,
    #: 2026-08-23). The two are separate buttons in Settings for the same
    #: reason.
    log_retention_days: int = Field(default=3, ge=0, le=365)
    chrome: ChromeConfig = Field(default_factory=ChromeConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    binaries: BinariesConfig = Field(default_factory=BinariesConfig)
    serve: ServeConfig = Field(default_factory=ServeConfig)
    brief: BriefConfig = Field(default_factory=BriefConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    guides: GuidesConfig = Field(default_factory=GuidesConfig)


def app_data_dir() -> Path:
    """`%APPDATA%/media-fetch-pipeline` (or `$APPDATA` equivalent)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / _APP_DIR_NAME
    # Non-Windows fallback so the module stays importable/testable off
    # Windows; the product itself targets Windows only (PSM §3).
    return Path.home() / f".{_APP_DIR_NAME}"


def config_path() -> Path:
    return app_data_dir() / _CONFIG_FILE_NAME


def default_config() -> AppConfig:
    return AppConfig()


def _announce_binaries(config: AppConfig) -> None:
    """Tell `mfp.toolchain` what the user configured, on every load.

    A side effect on another module, which wants justifying. `toolchain`
    resolves yt-dlp/ffmpeg for callers that have never had a config object
    to consult -- `stack.py` builds ffmpeg command lines in eight places and
    ignored `binaries.ffmpeg` in all eight, which was a defect nobody had
    named. Loading the config IS the moment the process learns what the user
    chose, so it is the honest moment to say so; the alternative was eight
    new parameters or eight call sites that keep being wrong.

    Imported here rather than at module scope: `toolchain` imports THIS
    module for `app_data_dir`, and a top-level import would be a cycle.
    """
    from mfp import toolchain

    toolchain.set_overrides(config.binaries)


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from `path` (default: the standard appdata location).

    Missing, unreadable, malformed, or schema-invalid files are treated
    identically: rebuild from defaults, persist the rebuilt file, and
    return it. This function never raises for a bad config file.
    """
    p = path or config_path()

    if p.exists():
        try:
            # utf-8-sig: this is a file the user is invited to edit by hand,
            # and Notepad's "UTF-8 with BOM" would otherwise land in the
            # rebuild-from-defaults branch below -- silently discarding every
            # setting they had just changed, with no error to explain it.
            raw = p.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            cfg = AppConfig.model_validate(data)
            _announce_binaries(cfg)
            return cfg
        except (OSError, json.JSONDecodeError, ValidationError):
            pass  # fall through to rebuild-from-defaults

    cfg = default_config()
    save_config(cfg, p)
    _announce_binaries(cfg)
    return cfg


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Persist `config` atomically (write to a temp file, then replace)."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(config.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
    os.replace(tmp, p)
