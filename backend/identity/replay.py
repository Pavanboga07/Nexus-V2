"""In-memory replay registry keyed by ``message_id``.

Only replay *primitives* live here: record an id, test membership,
atomically check-and-record, and drop expired entries. No persistence,
no gateway logic. The key is ``message_id`` (one physical message), not
``request_id`` (one logical operation shared by many messages).

Thread-safe via a single lock so concurrent ``check_and_record`` calls
admit exactly one winner. Timestamps reuse Component 1 semantics:
timezone-aware datetimes; naive values are rejected with ``ValueError``
(programming errors, not protocol errors).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from backend.protocol.types import is_valid_message_id


def _require_aware(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


class ReplayRegistry:
    """Thread-safe in-memory store of seen message ids with expiries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, datetime] = {}

    def _checked_id(self, message_id: object) -> str:
        if not is_valid_message_id(message_id):
            raise ValueError(f"invalid message_id: {message_id!r}")
        assert isinstance(message_id, str)
        return message_id

    def record(self, message_id: str, expires_at: datetime) -> None:
        """Record ``message_id`` as seen until ``expires_at``."""
        key = self._checked_id(message_id)
        expiry = _require_aware(expires_at, "expires_at")
        with self._lock:
            self._entries[key] = expiry

    def has_seen(
        self, message_id: str, now: Optional[datetime] = None
    ) -> bool:
        """Return True iff ``message_id`` is currently recorded.

        Lazily drops the entry when it has already expired, so a stale
        id never blocks unrelated traffic.
        """
        key = self._checked_id(message_id)
        current = (
            datetime.now(timezone.utc) if now is None
            else _require_aware(now, "now")
        )
        with self._lock:
            expiry = self._entries.get(key)
            if expiry is None:
                return False
            if current >= expiry:
                del self._entries[key]
                return False
            return True

    def check_and_record(
        self, message_id: str, expires_at: datetime,
        now: Optional[datetime] = None,
    ) -> bool:
        """Atomically admit ``message_id`` iff not already recorded.

        Returns True exactly once per id per window (the admitted
        caller); concurrent duplicates all receive False. An entry
        whose window has already passed is lazily dropped first, so a
        stale id never blocks re-admission — consistent with
        :meth:`has_seen`.
        """
        key = self._checked_id(message_id)
        expiry = _require_aware(expires_at, "expires_at")
        current = (
            datetime.now(timezone.utc) if now is None
            else _require_aware(now, "now")
        )
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if current >= existing:
                    del self._entries[key]
                else:
                    return False
            self._entries[key] = expiry
            return True

    def purge_expired(self, now: Optional[datetime] = None) -> int:
        """Remove entries past their window; return the removal count."""
        current = (
            datetime.now(timezone.utc) if now is None
            else _require_aware(now, "now")
        )
        with self._lock:
            expired = [
                key
                for key, expiry in self._entries.items()
                if current >= expiry
            ]
            for key in expired:
                del self._entries[key]
            return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
