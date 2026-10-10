"""Effective model resolution: saved provider config wins over env.

Turns and executions call here instead of using the startup runtime
directly, so a provider configured through Settings → AI Provider
actually drives the model. Without a saved configuration the startup
runtime (environment-based) is returned untouched, preserving the
existing behavior exactly.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

from backend.agent.config import AgentRuntimeConfig
from backend.agent.runtime import AgentRuntime
from backend.api import deps
from backend.provider_config.service import (
    ProviderConfigService,
    build_provider_model,
)


async def effective_runtime(request: Any, owner: UUID) -> Any | None:
    """Build the runtime for one owner (fresh model, shared shape).

    Returns the startup runtime unchanged when the owner has no saved
    provider configuration. Otherwise constructs an equivalent runtime
    whose model comes from the decrypted saved credentials, keeping
    the startup instance's tools and operational settings. Models are
    built per call (cheap constructors, no I/O) so key rotation and
    reconfiguration take effect immediately with nothing cached.
    """
    state = deps.state_of(request)
    store = ProviderConfigService(
        state.sessions, state.settings.credential_key)
    credentials = await store.load_credentials(owner)
    if credentials is None:
        return state.runtime
    provider, model, api_key = credentials
    chat_model = build_provider_model(provider, model, api_key)
    base = state.runtime
    if base is None:
        config = AgentRuntimeConfig(model=f"{provider}:{model}")
        tools: tuple = ()
    else:
        config = dataclasses.replace(
            base.config, model=f"{provider}:{model}")
        tools = base.tools
    return AgentRuntime(config, model=chat_model, tools=tools or None)
