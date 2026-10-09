"""Worker error model (dispatch failures, never secrets)."""

from __future__ import annotations


class WorkerError(Exception):
    """Base class for outbox-dispatcher failures."""


class MissingIdentityError(WorkerError):
    """No local identity is configured for the message's sender."""
