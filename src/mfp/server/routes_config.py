"""Config and doctor endpoints (PSM Batch 2 GUI §4.1, §9)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from mfp.config import AppConfig
from mfp.doctor import DoctorReport, run_doctor
from mfp.server.platform_gate import refresh_blocked_platforms


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

    return router
