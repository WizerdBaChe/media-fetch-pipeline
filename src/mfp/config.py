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


#: How a read-only lookup names its bucket: `lookup:youtube`, `lookup:bilibili`.
#:
#: A PREFIX rather than one shared `lookup` key, because the sliding window
#: and the minimum gap are properties of a SERVER. One key for every platform
#: would make a Bilibili question wait behind a YouTube one, which is pacing
#: against nobody. What the prefix shares is the LIMITS, not the window.
LOOKUP_PREFIX = "lookup:"


def lookup_bucket(platform: str) -> str:
    """The read-only bucket name for `platform`. One function, so the prefix
    is never spelt at a call site and a rename cannot half-happen."""
    return f"{LOOKUP_PREFIX}{platform}"


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
    #: Read-only lookups -- 「what does this video have」 with nothing landing
    #: on disk. User ruling 2026-09-09, after P-83 put these on the budget at
    #: all: a question that downloads nothing should not spend the same hour
    #: at the same cadence as a transfer.
    #:
    #: **The platform still sees one source**, and this bucket does not
    #: pretend otherwise -- separating them is OUR bookkeeping, not a promise
    #: about what the far end counts. So the numbers move by a factor of two,
    #: not by an order of magnitude: twice the hourly room, half the minimum
    #: gap. The cooldown is left at the default because nothing has measured
    #: how long a refusal lasts, and inventing that number is the tuning this
    #: project keeps deciding not to do on one data point.
    #:
    #: review-when: the empty-list condition of P-84 becomes reproducible and
    #: is shown to depend on request rate -- this bucket is then the first
    #: thing to tighten, and the comment above is why it was loosened.
    lookup: BudgetPlatformConfig = Field(
        default_factory=lambda: BudgetPlatformConfig(
            max_requests_per_hour=600,
            max_requests_per_run=200,
            min_interval_ms=500,
            jitter_ms=250,
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
        if platform.startswith(LOOKUP_PREFIX):
            return self.lookup
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
    #: Save the platform's own caption track beside the media, in the language
    #: the video was SPOKEN in (`captions.ORIGINAL_LANG`). The global default
    #: behind every queue row, overridable per row exactly as `policy` is.
    #:
    #: Off by default, matching `mfp fetch`: a caption track is a second file
    #: in the user's folder and a second thing to explain, so it is asked for.
    #: There is no language setting beside it on purpose -- naming a language
    #: asks the platform to TRANSLATE, which D-156/P-49 says has to be a
    #: deliberate per-run argument and never a stored default somebody set
    #: once and forgot.
    write_subs: bool = False
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
    guides: GuidesConfig = Field(default_factory=GuidesConfig)


#: Top-level `AppConfig` keys retired along with the transcript family.
#: `load_config` drops these from the parsed dict BEFORE validation, because
#: `AppConfig` forbids extra fields and a `ValidationError` on ANY field
#: rebuilds the WHOLE config from defaults -- an old config.json still
#: carrying `asr` would otherwise silently wipe outputRoot, policy and every
#: other setting on the next load. An explicit tuple only, never a generic
#: strip of unknown keys: that would hide a real typo in the user's file as
#: a bad-key drop instead of surfacing it as a validation error.
_RETIRED_KEYS = ("asr",)


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
            if isinstance(data, dict):
                for key in _RETIRED_KEYS:
                    data.pop(key, None)
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
