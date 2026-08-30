"""Fetch budget governor (PSM Batch 1 core §7).

A single, configurable, observable component -- not scattered `sleep`
calls (Phase 2 §2.4). State (the sliding hourly window, last-request time,
and any active cooldown) is persisted to
`%APPDATA%/media-fetch-pipeline/budget-state.json` so restarting the
process cannot reset the rate limit (§7: "otherwise reopening the app
zeroes it out and throttling becomes theater").

The clock, the sleep function, and the jitter RNG are all injected so
tests run against a simulated clock with zero real sleeping.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import Field as PydanticField
from pydantic import ValidationError

from mfp.config import AppConfig, app_data_dir
from mfp.errors import BudgetExhausted
from mfp.models import BudgetSnapshot, CamelModel

_STATE_FILE_NAME = "budget-state.json"
_WINDOW_SECONDS = 3600.0
_WARNING_THRESHOLD = 0.8

ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]
RandFn = Callable[[float, float], float]
EventEmitter = Callable[[dict[str, object]], None]


def default_state_path() -> Path:
    return app_data_dir() / _STATE_FILE_NAME


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _default_emit_event(event: dict[str, object]) -> None:
    # "Events are written to stderr as one JSON object per line" (§7).
    print(json.dumps(event), file=sys.stderr, flush=True)


# --- persisted state wire format -------------------------------------------


class _PersistedPlatformState(CamelModel):
    request_timestamps: list[float] = PydanticField(default_factory=list)
    last_request_at: float | None = None
    cooldown_until: float | None = None


class _PersistedState(CamelModel):
    schema_version: int = 1
    platforms: dict[str, _PersistedPlatformState] = PydanticField(default_factory=dict)


@dataclass
class _PlatformState:
    request_timestamps: list[float] = field(default_factory=list)
    requests_this_run: int = 0  # in-memory only; resets every process start
    last_request_at: float | None = None
    cooldown_until: float | None = None


class FetchBudgetGovernor:
    """Sliding-hour, per-run-capped, jittered, cooldown-aware request
    governor. One instance per process; state persists across processes.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        state_path: Path | None = None,
        clock: ClockFn = time.time,
        sleep: SleepFn = time.sleep,
        rand: RandFn = random.uniform,
        emit_event: EventEmitter | None = None,
    ) -> None:
        self._config = config
        self._state_path = state_path or default_state_path()
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._emit_event = emit_event or _default_emit_event
        self._warned_platforms: set[str] = set()
        self._platforms: dict[str, _PlatformState] = {}
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = self._state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            persisted = _PersistedState.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError):
            # Fail safe toward *more* throttling: an unreadable/corrupt
            # state file rebuilds as an empty window, never as "unlimited"
            # (PSM §13 Rollback).
            self._platforms = {}
            return

        self._platforms = {
            name: _PlatformState(
                request_timestamps=list(state.request_timestamps),
                last_request_at=state.last_request_at,
                cooldown_until=state.cooldown_until,
            )
            for name, state in persisted.platforms.items()
        }

    def _persist(self) -> None:
        persisted = _PersistedState(
            platforms={
                name: _PersistedPlatformState(
                    request_timestamps=state.request_timestamps,
                    last_request_at=state.last_request_at,
                    cooldown_until=state.cooldown_until,
                )
                for name, state in self._platforms.items()
            }
        )
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        tmp.write_text(persisted.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _get_state(self, platform: str) -> _PlatformState:
        if platform not in self._platforms:
            self._platforms[platform] = _PlatformState()
        return self._platforms[platform]

    @staticmethod
    def _prune_window(state: _PlatformState, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        state.request_timestamps = [ts for ts in state.request_timestamps if ts > cutoff]

    # -- public API ------------------------------------------------------

    def acquire(self, platform: str) -> None:
        """Reserve one request slot for `platform`, sleeping as needed to
        respect the configured cadence. Raises `BudgetExhausted` if the
        hourly cap, the per-run cap, or an active cooldown blocks it.
        """
        cfg = self._config.budget.for_platform(platform)
        state = self._get_state(platform)
        now = self._clock()

        if state.cooldown_until is not None and now < state.cooldown_until:
            raise BudgetExhausted(
                f"platform '{platform}' is in cooldown until {_iso(state.cooldown_until)}",
                platform=platform,
                cooldown_until=_iso(state.cooldown_until),
            )

        self._prune_window(state, now)

        if len(state.request_timestamps) >= cfg.max_requests_per_hour:
            raise BudgetExhausted(
                f"hourly request cap reached for platform '{platform}'",
                platform=platform,
            )

        if state.requests_this_run >= cfg.max_requests_per_run:
            raise BudgetExhausted(
                f"per-run request cap reached for platform '{platform}'",
                platform=platform,
            )

        occupancy = (
            len(state.request_timestamps) / cfg.max_requests_per_hour
            if cfg.max_requests_per_hour
            else 0.0
        )
        over_warning = occupancy >= _WARNING_THRESHOLD
        if over_warning and platform not in self._warned_platforms:
            self._emit_event(
                {
                    "event": "budget_warning",
                    "platform": platform,
                    "occupancy": occupancy,
                    "requestsUsedHour": len(state.request_timestamps),
                    "maxRequestsPerHour": cfg.max_requests_per_hour,
                }
            )
            self._warned_platforms.add(platform)
        elif not over_warning:
            self._warned_platforms.discard(platform)

        effective_min_interval_ms = cfg.min_interval_ms * 2 if over_warning else cfg.min_interval_ms

        if state.last_request_at is not None:
            elapsed_ms = (now - state.last_request_at) * 1000.0
            jitter_ms = self._rand(0.0, float(cfg.jitter_ms))
            required_gap_ms = effective_min_interval_ms + jitter_ms
            wait_ms = required_gap_ms - elapsed_ms
            if wait_ms > 0:
                self._sleep(wait_ms / 1000.0)

        # Re-read the clock after any wait: with a real clock this reflects
        # the actual post-sleep instant, so the *next* acquire() measures
        # elapsed time from when the request truly happened, not from when
        # this call started (which could understate the gap by the amount
        # just slept).
        request_time = self._clock()
        state.request_timestamps.append(request_time)
        state.requests_this_run += 1
        state.last_request_at = request_time
        self._persist()

    def report_block(self, platform: str) -> None:
        """Record a detected block signal for `platform`: enter cooldown
        and emit a `budget_blocked` event. Does not raise -- the caller
        (adapter/CLI orchestration) decides to abort the run with
        `stopReason: "blocked"` (PSM §7)."""
        cfg = self._config.budget.for_platform(platform)
        state = self._get_state(platform)
        now = self._clock()
        state.cooldown_until = now + (cfg.cooldown_on_block_ms / 1000.0)
        self._emit_event(
            {
                "event": "budget_blocked",
                "platform": platform,
                "cooldownUntil": _iso(state.cooldown_until),
            }
        )
        self._persist()

    def snapshot(self, platform: str) -> BudgetSnapshot:
        """Return the current observable state for `platform` without
        mutating request counters (pruning the sliding window is the only
        side effect, and it is idempotent/persisted)."""
        cfg = self._config.budget.for_platform(platform)
        state = self._get_state(platform)
        now = self._clock()
        self._prune_window(state, now)

        used_hour = len(state.request_timestamps)
        remaining_hour = max(0, cfg.max_requests_per_hour - used_hour)
        remaining_run = max(0, cfg.max_requests_per_run - state.requests_this_run)
        occupancy = used_hour / cfg.max_requests_per_hour if cfg.max_requests_per_hour else 0.0

        cooldown_active = state.cooldown_until is not None and state.cooldown_until > now
        cooldown_until_iso = _iso(state.cooldown_until) if cooldown_active else None

        next_allowed_at: str | None
        if cooldown_active:
            next_allowed_at = cooldown_until_iso
        elif state.last_request_at is not None:
            next_allowed_at = _iso(state.last_request_at + cfg.min_interval_ms / 1000.0)
        else:
            next_allowed_at = None

        return BudgetSnapshot(
            platform=platform,
            requests_used_hour=used_hour,
            requests_remaining_hour=remaining_hour,
            requests_used_run=state.requests_this_run,
            requests_remaining_run=remaining_run,
            next_allowed_at=next_allowed_at,
            warning_active=occupancy >= _WARNING_THRESHOLD,
            cooldown_until=cooldown_until_iso,
        )
