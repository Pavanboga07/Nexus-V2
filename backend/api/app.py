"""Component 12 application: composition root and lifecycle.

:func:`create_app` builds the FastAPI application over an explicit
:class:`ApiState`. Production wiring lives in
:func:`build_production_state`; tests inject fakes through the same
``ApiState`` fields instead. Lifespan owns every resource it starts
(database engine, MCP manager, gateway client, dispatcher task,
worker task) and releases them in reverse on shutdown; a startup
failure disposes whatever was already opened.

No route handler ever constructs services directly: everything comes
from ``request.app.state``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.agent.config import AgentRuntimeConfig
from backend.agent.conversation_service import ConversationService
from backend.agent.runtime import AgentRuntime
from backend.api.errors import ApiError
from backend.api.routes import agents, conversations, health, requests
from backend.api.settings import ApiSettings
from backend.identity.keys import private_key_from_bytes
from backend.identity.models import AgentIdentity
from backend.inbound import InboundDispatcher, InboundProcessor
from backend.mcp.config import MCPManagerConfig
from backend.mcp.manager import MCPManager
from backend.persistence.database import (
    check_connectivity,
    create_engine,
    create_session_factory,
    dispose_engine,
    get_database_url,
)
from backend.requests.service import RequestService
from backend.responses import ResponseReceiver

logger = logging.getLogger("nexus.api")


@dataclass
class ApiState:
    """Everything one API process owns (single ownership each)."""

    settings: ApiSettings
    sessions: Any
    engine: Any = None
    conversations: Any = None
    runtime: Any = None
    identities: Any = None
    gateway_clients: dict = field(default_factory=dict)
    dispatchers: list = field(default_factory=list)
    worker: Any = None
    worker_task: Any = None
    mcp_manager: Any = None


class FileIdentityProvider:
    """Single-owner key provider backed by an explicit JSON file.

    File format is ``{agent_id: base64-private-key-bytes}``. The file
    is read once at startup; keys never leave this object except as
    ``Ed25519PrivateKey`` values handed to local signing operations.
    Dev-grade handling for the single-owner model: restrict file
    permissions and rotate by replacing the file and restarting.
    """

    def __init__(self, identities: dict[str, Any]) -> None:
        self._identities = dict(identities)

    @classmethod
    def from_file(cls, path: str) -> "FileIdentityProvider":
        """Load and validate every entry (fail closed on any problem)."""
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"identity keys file {path!r} is unreadable"
            ) from exc
        if not isinstance(data, dict) or not data:
            raise ValueError(
                f"identity keys file {path!r} must be a non-empty object"
            )
        identities: dict[str, Any] = {}
        for agent_id, encoded in data.items():
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError(
                    f"identity keys file {path!r} has an invalid agent id"
                )
            if not isinstance(encoded, str) or not encoded:
                raise ValueError(
                    f"identity keys file {path!r} has an invalid key "
                    f"for {agent_id!r}"
                )
            try:
                raw = base64.b64decode(encoded, validate=True)
                private_key = private_key_from_bytes(raw)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    f"identity keys file {path!r} has an undecodable key "
                    f"for {agent_id!r}"
                ) from exc
            identities[agent_id] = AgentIdentity(
                agent_id=agent_id, private_key=private_key)
        return cls(identities)

    def private_key_for(self, agent_id: str) -> Any:
        """Return the private key for ``agent_id`` (KeyError if unknown)."""
        return self._identities[agent_id].private_key

    def identity_for(self, agent_id: str) -> AgentIdentity:
        """Return the full identity for ``agent_id``."""
        return self._identities[agent_id]

    @property
    def agent_ids(self) -> list[str]:
        """Configured agent ids (for gateway client wiring)."""
        return sorted(self._identities)


def _build_runtime(tools: list[Any] | None) -> AgentRuntime | None:
    """Build the shared runtime, or None when no model is configured."""
    config = AgentRuntimeConfig.from_env()
    if not config.model:
        logger.warning("no NEXUS_LLM_MODEL configured; model routes idle")
        return None
    return AgentRuntime(config, tools=tools)


async def build_production_state(
    settings: ApiSettings,
) -> ApiState:
    """Wire real services from the declared environment (gated pieces).

    Always wired: database. Model-backed pieces (runtime,
    conversations, execution identities) only when their
    configuration is present; gateway live I/O only when explicitly
    enabled. Anything unconfigured surfaces as HTTP 503 at the routes
    that need it — never as a half-built service.
    """
    engine = create_engine(get_database_url())
    sessions = create_session_factory(engine)
    state = ApiState(settings=settings, sessions=sessions, engine=engine)
    try:
        await check_connectivity(engine)
        await _open_optional(state, settings)
    except BaseException:
        await close_state(state)
        raise
    return state


async def _open_optional(state: ApiState, settings: ApiSettings) -> None:
    """Open model/MCP/identity/gateway pieces; degrade loudly by piece."""
    tools: list[Any] | None = None
    if settings.mcp_config_path is not None:
        manager = MCPManager(
            MCPManagerConfig.from_file(settings.mcp_config_path))
        await manager.open()
        state.mcp_manager = manager
        tools = list(manager.allowed_tools())
    state.runtime = _build_runtime(tools)
    if settings.identity_keys_path is not None:
        state.identities = FileIdentityProvider.from_file(
            settings.identity_keys_path)
    if state.runtime is not None:
        state.conversations = ConversationService(
            state.sessions, state.runtime)
    if settings.enable_gateway_io:
        await _open_gateway_io(state, settings)


async def _open_gateway_io(state: ApiState, settings: ApiSettings) -> None:
    """Connect gateway clients, dispatchers, receiver, worker (opt-in)."""
    from backend.gateway_client.client import GatewayClient
    from backend.worker.config import WorkerConfig
    from backend.worker.outbox_dispatcher import OutboxDispatcher

    if state.identities is None:
        raise ValueError(
            "gateway I/O requires NEXUS_API_IDENTITY_KEYS_PATH"
        )
    provider = state.identities
    for agent_id in provider.agent_ids:
        client = GatewayClient(
            provider.identity_for(agent_id), settings.gateway_url)
        await client.connect()
        state.gateway_clients[agent_id] = client
        processor = InboundProcessor(state.sessions, agent_id)
        dispatcher, receive = InboundDispatcher.from_client(
            client, processor)
        receiver = ResponseReceiver(
            state.sessions, agent_id, provider)
        await receiver.attach(dispatcher)
        task = dispatcher.start(receive)
        state.dispatchers.append((dispatcher, task, receiver))
    service = RequestService(state.sessions, provider)

    def factory(sender_id: str) -> Any:
        client = state.gateway_clients.get(sender_id)
        if client is None:
            raise ValueError(
                f"no gateway client for {sender_id!r}")
        return client

    worker = OutboxDispatcher(
        sessions=state.sessions,
        identities=provider,
        gateway_url=settings.gateway_url,
        service=service,
        config=WorkerConfig(),
        client_factory=factory,
    )
    state.worker = worker
    state.worker_task = asyncio.get_running_loop().create_task(
        worker.run_forever())


async def close_state(state: ApiState) -> None:
    """Release everything in reverse order (idempotent, never raises)."""
    if state.worker is not None:
        try:
            await state.worker.stop()
        except Exception:  # noqa: BLE001 - shutdown must not fail
            logger.warning("worker shutdown failed")
        state.worker = None
    if state.worker_task is not None:
        task, state.worker_task = state.worker_task, None
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    for dispatcher, task, _receiver in reversed(state.dispatchers):
        try:
            await dispatcher.stop()
        except Exception:  # noqa: BLE001 - shutdown must not fail
            logger.warning("dispatcher shutdown failed")
    state.dispatchers.clear()
    for client in state.gateway_clients.values():
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - shutdown must not fail
            logger.warning("gateway client shutdown failed")
    state.gateway_clients.clear()
    if state.mcp_manager is not None:
        try:
            await state.mcp_manager.close()
        except Exception:  # noqa: BLE001 - shutdown must not fail
            logger.warning("MCP shutdown failed")
        state.mcp_manager = None
    if state.engine is not None:
        try:
            await dispose_engine(state.engine)
        except Exception:  # noqa: BLE001 - shutdown must not fail
            logger.warning("engine shutdown failed")
        state.engine = None


def create_app(
    settings: ApiSettings | None = None,
    *,
    state: ApiState | None = None,
) -> FastAPI:
    """Build the FastAPI application (injectable state for tests)."""
    resolved = settings or ApiSettings.from_env()
    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        if application.state.api is None:
            application.state.api = await build_production_state(resolved)
        try:
            yield
        finally:
            current = application.state.api
            application.state.api = None
            if current is not None:
                await close_state(current)

    application = FastAPI(
        title="Nexus API",
        version="1",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )
    application.state.settings = resolved
    application.state.api = state
    application.include_router(health.router, prefix="/api/v1")
    application.include_router(agents.router, prefix="/api/v1")
    application.include_router(conversations.router, prefix="/api/v1")
    application.include_router(requests.router, prefix="/api/v1")

    @application.exception_handler(ApiError)
    async def _api_error_handler(
        request: Request, exc: ApiError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "message": exc.message},
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Framework body validation: same envelope as domain errors,
        # without echoing the offending payload.
        return JSONResponse(
            status_code=422,
            content={"code": "INVALID_INPUT",
                     "message": "request input is invalid"},
        )

    @application.exception_handler(StarletteHTTPException)
    async def _http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": code, "message": "request failed"},
        )

    @application.exception_handler(Exception)
    async def _unexpected_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled API failure")
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": "internal error"},
        )

    return application


def _default_app() -> FastAPI:
    """Module-level application (never fails on malformed environment)."""
    try:
        return create_app()
    except ValueError as exc:
        logger.warning("invalid API environment, using defaults: %s", exc)
        return create_app(ApiSettings())


app = _default_app()
