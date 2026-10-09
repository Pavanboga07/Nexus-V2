"""Repository helpers and public repository surface.

Repositories are thin data-access objects bound to one explicit
``AsyncSession``. Integrity failures map to :class:`ConflictError`
(duplicate/unique violations) or :class:`PersistenceError` (foreign
key, check, not-null, oversized data); nothing here decides whether a
write *should* happen — that policy belongs to later services.

A failed flush poisons its transaction, so every mutating method rolls
back before raising: the caller's session stays usable and a surrounding
``session_scope`` can still commit unrelated work.
"""

from __future__ import annotations

from typing import NoReturn

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import ConflictError, PersistenceError


async def _flush(session: AsyncSession) -> None:
    """Flush, mapping database failures to persistence exceptions.

    Integrity violations become :class:`ConflictError` (duplicate key)
    or :class:`PersistenceError` (foreign key, check, not-null); any
    other SQLAlchemy failure (oversized data, unserializable values,
    lost connection) becomes :class:`PersistenceError` chained to the
    original, so internal callers keep full diagnostics.
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        _reraise_integrity(exc)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise PersistenceError(f"database write failed: {exc}") from exc


def _reraise_integrity(exc: IntegrityError) -> NoReturn:
    """Map a SQLAlchemy integrity failure to a persistence exception."""
    pgcode = getattr(exc.orig, "sqlstate", "") or ""
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
    detail = f" ({constraint})" if constraint else ""
    if pgcode == "23505":
        raise ConflictError(f"duplicate record{detail}") from exc
    raise PersistenceError(
        f"integrity violation{detail}: sqlstate {pgcode or 'unknown'}"
    ) from exc


from .agents import AgentRepository  # noqa: E402
from .approvals import ApprovalRepository  # noqa: E402
from .audit_events import AuditEventRepository  # noqa: E402
from .conversations import (  # noqa: E402
    ConversationMessageRepository,
    ConversationRepository,
)
from .messages import MessageRepository  # noqa: E402
from .outbox import OutboxRepository  # noqa: E402
from .peers import PeerRepository  # noqa: E402
from .remote_requests import RemoteRequestRepository  # noqa: E402
from .users import UserRepository  # noqa: E402

__all__ = [
    "UserRepository",
    "AgentRepository",
    "PeerRepository",
    "RemoteRequestRepository",
    "MessageRepository",
    "OutboxRepository",
    "ApprovalRepository",
    "ConversationRepository",
    "ConversationMessageRepository",
    "AuditEventRepository",
]
