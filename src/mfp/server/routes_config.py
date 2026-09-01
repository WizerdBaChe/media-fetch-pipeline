"""Config and doctor endpoints (PSM Batch 2 GUI §4.1, §9)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mfp.config import AppConfig, GuidesConfig
from mfp.doctor import DoctorReport, run_doctor
from mfp.server.platform_gate import refresh_blocked_platforms


class GuideRequest(BaseModel):
    id: str


def build_config_router() -> APIRouter:
    router = APIRouter(tags=["config"])

    @router.get("/config", response_model=AppConfig)
    async def get_config(request: Request) -> AppConfig:
        return request.app.state.config

    @router.put("/config", response_model=AppConfig)
    async def put_config(request: Request, body: AppConfig) -> AppConfig:
        request.app.state.config = body
        saver = request.app.state.save_config
        if saver is not None:
            saver(body)
        return body

    @router.get("/doctor", response_model=DoctorReport)
    async def doctor(request: Request) -> DoctorReport:
        report = run_doctor(request.app.state.config)
        # Reuse the report we just paid for: installing a newer yt-dlp and
        # re-opening this panel is how a user expects the block to lift.
        refresh_blocked_platforms(request.app, report)
        return report

    # --- one-time explanations ---------------------------------------------
    #
    # Three small endpoints rather than letting the panel PUT the whole
    # config: marking a guide as seen is an APPEND, and `PUT /v1/config`
    # replaces the object wholesale. Two guides dismissed in the same second
    # -- which is exactly what happens on a first launch -- would each send
    # the config they read before the other's write, and the second would
    # silently drop the first. The read-modify-write belongs on the side
    # that owns the file.

    def _save(request: Request, guides: GuidesConfig) -> GuidesConfig:
        config = request.app.state.config.model_copy(update={"guides": guides})
        request.app.state.config = config
        saver = request.app.state.save_config
        if saver is not None:
            saver(config)
        return guides

    @router.get("/guides", response_model=GuidesConfig)
    async def get_guides(request: Request) -> GuidesConfig:
        return request.app.state.config.guides

    @router.post("/guides:seen", response_model=GuidesConfig)
    async def mark_seen(request: Request, body: GuideRequest) -> GuidesConfig:
        seen = list(request.app.state.config.guides.seen)
        if body.id not in seen:
            seen.append(body.id)
        return _save(request, GuidesConfig(seen=seen))

    @router.post("/guides:reset", response_model=GuidesConfig)
    async def reset_guides(request: Request) -> GuidesConfig:
        """Show every explanation again.

        Here because 「我按太快，那個說明可以叫回來嗎」 has exactly one honest
        answer, and it is not "delete a file in %APPDATA%".
        """
        return _save(request, GuidesConfig())

    return router
