"""AI provider configuration endpoints (owner-scoped, keyless I/O).

The API key enters through ``PUT`` and leaves the process only toward
the provider during an explicit test or model run. ``GET`` returns
metadata and a configured flag; no endpoint returns, logs, or echoes
the key. Deletes are immediate and total.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.api import deps
from backend.api.errors import ApiError, to_api_error
from backend.api.schemas import (
    ProviderSave,
    ProviderStatus,
    ProviderTest,
    ProviderTestResult,
)
from backend.provider_config.errors import ProviderConfigError
from backend.provider_config.service import ProviderConfigService

router = APIRouter(tags=["provider"])


def _service_of(request: Request) -> ProviderConfigService:
    state = deps.state_of(request)
    return ProviderConfigService(
        state.sessions, state.settings.credential_key)


@router.get("/settings/ai-provider", response_model=ProviderStatus)
async def provider_status(
    request: Request, settings=Depends(deps.authenticate),
) -> ProviderStatus:
    """Configured flag plus non-secret metadata (never the key)."""
    try:
        public = await _service_of(request).get_public(
            settings.owner_user_id)
    except ProviderConfigError as exc:
        raise to_api_error(exc)
    return ProviderStatus(**public)


@router.put("/settings/ai-provider", response_model=ProviderStatus)
async def provider_save(
    body: ProviderSave,
    request: Request,
    settings=Depends(deps.authenticate),
) -> ProviderStatus:
    """Validate, encrypt, and store one owner's configuration."""
    try:
        public = await _service_of(request).save(
            settings.owner_user_id,
            body.provider,
            body.model,
            body.api_key,
        )
    except ProviderConfigError as exc:
        raise to_api_error(exc)
    return ProviderStatus(**public)


@router.delete("/settings/ai-provider")
async def provider_delete(
    request: Request, settings=Depends(deps.authenticate),
) -> dict:
    """Remove one owner's configuration; True when one existed."""
    try:
        removed = await _service_of(request).delete(
            settings.owner_user_id)
    except ProviderConfigError as exc:
        raise to_api_error(exc)
    return {"removed": removed}


@router.post("/settings/ai-provider/test",
             response_model=ProviderTestResult)
async def provider_test(
    body: ProviderTest,
    request: Request,
    settings=Depends(deps.authenticate),
) -> ProviderTestResult:
    """Run one minimal live provider call (saved or candidate values).

    With explicit fields the candidate is tested without storing
    anything; otherwise the saved configuration is used. Either way
    the response carries only provider, model, latency, and a short
    reply preview — never credentials.
    """
    service = _service_of(request)
    try:
        if (body.provider is not None or body.model is not None
                or body.api_key is not None):
            if (body.provider is None or body.model is None
                    or body.api_key is None):
                raise ApiError(
                    422, "INVALID_INPUT", "request input is invalid")
            result = await service.test_candidate(
                body.provider, body.model, body.api_key)
        else:
            result = await service.test_saved(settings.owner_user_id)
    except ProviderConfigError as exc:
        raise to_api_error(exc)
    return ProviderTestResult(**result)
