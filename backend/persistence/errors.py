"""Component 4 error model (data-layer failures only, no business policy)."""

from __future__ import annotations


class PersistenceError(Exception):
    """Base class for persistence-layer failures."""


class ConflictError(PersistenceError):
    """A uniqueness or exclusion constraint rejected the write
    (duplicate id, duplicate relation, duplicate approval)."""


class DatabaseConnectionError(PersistenceError):
    """The database could not be reached or the connection failed."""
