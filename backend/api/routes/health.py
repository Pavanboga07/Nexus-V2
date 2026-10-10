"""Component 12 health endpoints (explicitly unauthenticated).

Liveness proves the process serves; readiness proves the database is
reachable. Neither carries credentials, connection strings, or error
detail: readiness reports a bare boolean outcome only.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from backend.api import deps
from backend.api.schemas import HealthBody

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthBody)
async def live() -> HealthBody:
    """Process liveness (no dependencies checked)."""
    return HealthBody(status="ok")


@router.get(
    "/health/ready",
    response_model=HealthBody,
    responses={503: {"description": "A dependency is unreachable"}},
)
async def ready(request: Request) -> HealthBody:
    """Readiness: the database answers a trivial query."""
    from fastapi.responses import JSONResponse

    state = deps.state_of(request)
    try:
        async with state.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - readiness is boolean by design
        return JSONResponse(
            status_code=503, content={"status": "not-ready"})
    return HealthBody(status="ready")
