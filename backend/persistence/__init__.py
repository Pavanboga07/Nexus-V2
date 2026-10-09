"""Nexus Persistence (Component 4): PostgreSQL data-access layer.

Durable storage for users, agents, peers, remote requests, protocol
messages, approvals, conversations, and audit events. Data in, data
out — no workflows, no policy, no transport, no language-model logic.
"""

from .database import (
    check_connectivity,
    create_engine,
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    get_database_url,
    session_scope,
)
from .errors import (
    ConflictError,
    DatabaseConnectionError,
    PersistenceError,
)

__all__ = [
    "get_database_url",
    "create_engine",
    "create_session_factory",
    "check_connectivity",
    "session_scope",
    "ensure_database_exists",
    "dispose_engine",
    "PersistenceError",
    "ConflictError",
    "DatabaseConnectionError",
]
