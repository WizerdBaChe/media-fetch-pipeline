"""SSE broadcaster (PSM Batch 2 GUI §4.4).

One-way, server-to-client, over ordinary HTTP: SSE reconnects on its own and
needs no heartbeat protocol of ours. Commands travel as normal POSTs, so the
bidirectional half of WebSocket would be dead weight.

Two behaviours here are deliberate rather than incidental:

- **Per-task coalescing at 4 Hz.** A byte-counter firing at transfer speed
  would flood the stream and jank the table at the queue sizes this app is
  built for. Only `task` events coalesce; state changes and notices are
  never dropped (see `publish`).
- **Bounded per-subscriber queues.** A slow or wedged client must not grow
  memory without limit. On overflow the OLDEST pending event is dropped:
  for a progress stream the newest value is the truthful one.
- **A heartbeat comment every few seconds.** `EventSource` is documented to
  reconnect on its own, and that is why SSE was chosen -- but "the socket
  broke" is not a fact the browser can be relied on to observe promptly when
  a proxy sits in between. Measured 2026-08-16 (R5-8): killing the API left
  the dev proxy logging `ECONNRESET` while the page's stream indicator sat on
  green for over 30 seconds. A periodic frame turns liveness into something
  the client can time out on itself, independent of any socket event.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Callable

ClockFn = Callable[[], float]

#: Minimum seconds between two `task` events for the same task id.
COALESCE_INTERVAL_S = 0.25

#: Per-subscriber backlog before the oldest event is dropped.
MAX_QUEUED_EVENTS = 512

#: Seconds between heartbeat frames on an otherwise idle stream. The client
#: declares the stream stale at a multiple of this (see `shared/api/events.ts`),
#: so the two numbers are one contract: changing this without changing that
#: makes the UI either cry wolf or stay green over a dead socket.
HEARTBEAT_INTERVAL_S = 5.0


def format_sse(event: str, data: Any) -> str:
    """Render one SSE frame. `data` is JSON on a single line."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


class EventBroadcaster:
    def __init__(self, *, clock: ClockFn = time.monotonic) -> None:
        self._clock = clock
        self._subscribers: list[asyncio.Queue[tuple[str, Any]]] = []
        self._last_task_emit: dict[str, float] = {}

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[tuple[str, Any]]:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def publish(self, event: str, data: Any, *, coalesce_key: str | None = None) -> bool:
        """Broadcast one event. Returns False if it was coalesced away.

        `coalesce_key` (the task id, for `task` events) enables rate
        limiting. A caller that must not be dropped -- a state change, a
        completion, an error -- simply omits it. That is the whole
        safeguard: progress is throttled, meaning is not.
        """
        if coalesce_key is not None:
            now = self._clock()
            last = self._last_task_emit.get(coalesce_key)
            if last is not None and (now - last) < COALESCE_INTERVAL_S:
                return False
            self._last_task_emit[coalesce_key] = now

        for queue in self._subscribers:
            if queue.full():
                # Drop the OLDEST: for progress, the newest value is the true one.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race-only path
                    pass
            queue.put_nowait((event, data))
        return True

    def forget(self, coalesce_key: str) -> None:
        """Drop a task's rate-limit state once it leaves the queue."""
        self._last_task_emit.pop(coalesce_key, None)

    async def stream(
        self,
        queue: asyncio.Queue[tuple[str, Any]],
        *,
        heartbeat_s: float = HEARTBEAT_INTERVAL_S,
    ) -> AsyncIterator[str]:
        """Yield SSE frames until the client disconnects.

        Idle time produces a `ping` event rather than nothing, so the client
        can distinguish "quiet queue" from "dead connection". It is a real
        event and not a bare comment because a comment is invisible to
        `EventSource` listeners -- the client must be able to observe it.
        """
        try:
            # An immediate comment frame makes proxies flush and lets the
            # client know the stream is live before the first real event.
            yield ": connected\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                except asyncio.TimeoutError:
                    yield format_sse("ping", {"t": round(self._clock(), 3)})
                    continue
                yield format_sse(event, data)
        finally:
            self.unsubscribe(queue)
