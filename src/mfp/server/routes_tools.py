"""The two programs this product cannot work without, and the button that
gets them.

Why this is a router and not three lines inside `routes_config`: installing
a tool is a long transfer that reports itself the whole way, which is the
same shape as the model download and nothing like reading a config field.
`GET /v1/doctor` still answers "is it there"; this answers "get it", and
「哪一份在生效」, which doctor's `path` alone cannot say.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mfp import toolchain
from mfp.models import CamelModel

#: One event name for every managed install, coalesced like the model one.
#: A second install cannot be in flight -- the panel disables its buttons as
#: a group -- so a per-tool channel would buy nothing.
TOOL_EVENT = "toolInstall"


class ToolsResponse(CamelModel):
    tools: list[toolchain.ToolStatus]


class ToolRequest(BaseModel):
    name: str


def _progress_publisher(request: Request):
    """Same shape and same reasons as `routes_asr._progress_publisher`."""
    broadcaster = getattr(request.app.state, "broadcaster", None)
    if broadcaster is None:
        return None
    dispatch = getattr(request.app.state, "dispatch", None) or (lambda fn: fn())

    def publish(record: dict) -> None:
        dispatch(
            lambda: broadcaster.publish(TOOL_EVENT, record, coalesce_key=TOOL_EVENT)
        )

    return publish


def build_tools_router() -> APIRouter:
    # No prefix, full paths: `/tools:install` is a verb ON the collection,
    # the spelling `routes_queue` already uses for `/queue:startAll`, and a
    # router prefix cannot express it -- a route path must start with `/`.
    router = APIRouter(tags=["tools"])

    @router.get("/tools", response_model=ToolsResponse)
    def list_tools(request: Request) -> ToolsResponse:
        # Config overrides are re-announced on every read rather than only
        # at startup: `PUT /v1/config` can change `binaries.*` while the app
        # is open, and a panel that kept reporting the previous answer would
        # be the one place in the product still showing a setting the user
        # had already changed.
        config = request.app.state.config
        toolchain.set_overrides(config.binaries)
        return ToolsResponse(tools=toolchain.statuses(chrome=config.chrome))

    @router.post("/tools:install", response_model=toolchain.ToolStatus)
    def install(request: Request, body: ToolRequest) -> toolchain.ToolStatus:
        """Fetch, verify and place one tool. Minutes for ffmpeg, and said so.

        Never called on the app's behalf -- only when a person presses a
        button. A hundred megabytes moving on its own looks exactly like a
        hang, which is the reasoning `allowDownload` already encodes for
        models.
        """
        toolchain.set_overrides(request.app.state.config.binaries)
        return toolchain.install(body.name, on_progress=_progress_publisher(request))

    @router.post("/tools:remove", response_model=toolchain.ToolStatus)
    def remove(request: Request, body: ToolRequest) -> toolchain.ToolStatus:
        """Drop the copy this program installed. Nothing else is touched."""
        toolchain.set_overrides(request.app.state.config.binaries)
        return toolchain.remove(body.name)

    return router
