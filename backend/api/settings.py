"""Component 12 API settings: explicit environment configuration.

All deployment knobs arrive here; nothing reads ``os.environ``
outside this module (service configs keep their own ``from_env``
conventions, composed below). Authentication is a single-owner
bearer token bound to one ``owner_user_id``: there is deliberately
no multi-user account system in this version.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any


def _get(environ, name: str) -> str | None:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


@dataclass(frozen=True)
class ApiSettings:
    """Deployment configuration for one API process."""

    # Bearer token gating every private endpoint (never logged).
    api_token: str | None = None
    # The single user id the token acts as (UUID string in env).
    owner_user_id: uuid.UUID | None = None
    # Loopback-only serving by default; never 0.0.0.0 implicitly.
    host: str = "127.0.0.1"
    port: int = 8000
    # Gateway live I/O (client + dispatcher + receiver + worker loop).
    # Off unless explicitly enabled: no surprise network or consumers.
    enable_gateway_io: bool = False
    gateway_url: str = "ws://127.0.0.1:8765"
    # Optional MCP tool configuration file (runtime stays tool-free
    # when unset).
    mcp_config_path: str | None = None
    # JSON file mapping agent_id -> base64 private-key bytes for the
    # execution endpoint's IdentityProvider (dev-grade single-owner
    # key material handling; production hardening deferred).
    identity_keys_path: str | None = None
    # Master key for provider-credential encryption at rest (Fernet
    # urlsafe-b64). Absent means the credential store is unavailable:
    # saves fail closed and nothing is ever written in plaintext.
    credential_key: str | None = None
    # Extra knobs with safe defaults (tests override the object).
    extra: dict = field(default_factory=dict)
    # Explicit browser origins allowed on the WebSocket endpoint, in
    # addition to same-origin requests. Empty means same-origin (plus
    # origin-less non-browser clients) only; never "*".
    allowed_origins: tuple = ()
    # WebSocket ticket lifetime in seconds (short by design).
    ticket_ttl_seconds: float = 60.0

    @classmethod
    def from_env(
        cls, environ: Any = None, prefix: str = "NEXUS_API_"
    ) -> "ApiSettings":
        """Build settings from environment (composition-root entry)."""
        env = environ if environ is not None else os.environ
        owner_raw = _get(env, prefix + "OWNER_USER_ID")
        owner: uuid.UUID | None = None
        if owner_raw is not None:
            try:
                owner = uuid.UUID(owner_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{prefix}OWNER_USER_ID must be a UUID"
                ) from exc
        port_raw = _get(env, prefix + "PORT")
        port = 8000
        if port_raw is not None:
            try:
                port = int(port_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{prefix}PORT must be an integer"
                ) from exc
            if not 1 <= port <= 65535:
                raise ValueError(f"{prefix}PORT must be 1-65535")
        flag_raw = _get(env, prefix + "ENABLE_GATEWAY_IO")
        origins_raw = _get(env, prefix + "ALLOWED_ORIGINS")
        ttl_raw = _get(env, prefix + "TICKET_TTL_S")
        ttl = 60.0
        if ttl_raw is not None:
            try:
                ttl = float(ttl_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{prefix}TICKET_TTL_S must be a number"
                ) from exc
            if ttl <= 0:
                raise ValueError(
                    f"{prefix}TICKET_TTL_S must be positive")
        return cls(
            api_token=_get(env, prefix + "TOKEN"),
            owner_user_id=owner,
            host=_get(env, prefix + "HOST") or "127.0.0.1",
            port=port,
            enable_gateway_io=(
                flag_raw.lower() in ("1", "true", "yes", "on")
                if flag_raw is not None
                else False
            ),
            gateway_url=(
                _get(env, prefix + "GATEWAY_URL") or "ws://127.0.0.1:8765"
            ),
            mcp_config_path=_get(env, prefix + "MCP_CONFIG_PATH"),
            identity_keys_path=_get(env, prefix + "IDENTITY_KEYS_PATH"),
            credential_key=_get(env, prefix + "CREDENTIAL_KEY"),
            allowed_origins=tuple(
                origin.strip() for origin in (origins_raw or "").split(",")
                if origin.strip()
            ),
            ticket_ttl_seconds=ttl,
        )

    @property
    def auth_configured(self) -> bool:
        """True only when token and owner binding are both present."""
        return self.api_token is not None and self.owner_user_id is not None
