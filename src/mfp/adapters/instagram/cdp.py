"""The real CDP transport behind `chrome.CdpConnection` (PSM §5.3).

Kept apart from `chrome.py` so the launch/lifecycle rules — which are the
easy ones to get silently wrong — stay testable without a socket. This module
is the thin, boring half: frame in, frame out.

Synchronous on purpose. The acquisition path is one page at a time by
design (the budget governor rations it), so an event loop here would buy
concurrency the product is not allowed to use, and would make the `finally`
that guarantees no orphan Chrome harder to reason about.
"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import httpx
from websockets.sync.client import connect as ws_connect

from mfp.errors import CdpTimeoutError

#: CDP payloads are large: a carousel's outerHTML runs to megabytes, and the
#: default frame limit would truncate it into a parse error that looks like a
#: structure change.
MAX_FRAME_BYTES = 64 * 1024 * 1024

#: Seconds allowed for `GET /json/version`. Chrome is local and already
#: listening by the time we ask, so this only has to cover a wedged browser.
HTTP_TIMEOUT_S = 10.0


def http_get(url: str) -> str:
    """Plain GET against Chrome's local HTTP endpoint."""
    try:
        response = httpx.get(url, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        raise CdpTimeoutError(f"could not reach Chrome at {url}: {exc}") from exc


class WebsocketCdpConnection:
    """One browser-level CDP websocket.

    Replies and events share the socket, so `send` reads until it sees the
    reply carrying its own id, buffering any events that arrive meanwhile.
    Dropping them instead would lose `Page.loadEventFired` whenever it
    happened to arrive between a command and its reply -- which is exactly
    when it usually does.
    """

    def __init__(self, url: str, *, open_timeout: float = 10.0) -> None:
        try:
            self._socket = ws_connect(
                url, max_size=MAX_FRAME_BYTES, open_timeout=open_timeout
            )
        except Exception as exc:  # noqa: BLE001 -- websockets raises a wide family
            raise CdpTimeoutError(f"could not open a CDP session at {url}: {exc}") from exc
        self._ids = count(1)
        self._pending_events: list[dict[str, Any]] = []

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        message_id = next(self._ids)
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        self._socket.send(json.dumps(message))

        while True:
            frame = self._recv(timeout, context=method)
            if frame.get("id") != message_id:
                if "method" in frame:
                    self._pending_events.append(frame)
                continue
            if "error" in frame:
                raise CdpTimeoutError(f"CDP {method} failed: {frame['error']}")
            return frame.get("result", {})

    def wait_for_event(self, method: str, timeout: float = 30.0) -> dict[str, Any]:
        for index, frame in enumerate(self._pending_events):
            if frame.get("method") == method:
                return self._pending_events.pop(index).get("params", {})

        while True:
            frame = self._recv(timeout, context=method)
            if frame.get("method") == method:
                return frame.get("params", {})
            if "method" in frame:
                self._pending_events.append(frame)

    def _recv(self, timeout: float, *, context: str) -> dict[str, Any]:
        try:
            raw = self._socket.recv(timeout=timeout)
        except TimeoutError as exc:
            raise CdpTimeoutError(f"CDP timed out waiting for {context}") from exc
        except Exception as exc:  # noqa: BLE001 -- a closed socket lands here too
            raise CdpTimeoutError(f"CDP connection lost while waiting for {context}: {exc}") from exc
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CdpTimeoutError(f"CDP sent an unreadable frame: {raw!r:.200}") from exc
        if not isinstance(frame, dict):
            raise CdpTimeoutError(f"CDP sent a {type(frame).__name__}, expected an object")
        return frame

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:  # noqa: BLE001 -- already gone is the common case
            pass


def connect(url: str) -> WebsocketCdpConnection:
    """`chrome.Connector` implementation."""
    return WebsocketCdpConnection(url)


__all__ = ["HTTP_TIMEOUT_S", "MAX_FRAME_BYTES", "WebsocketCdpConnection", "connect", "http_get"]
