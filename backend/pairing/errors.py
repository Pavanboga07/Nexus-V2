"""Typed pairing failures (mapped to HTTP in api.errors)."""


class PairingError(Exception):
    """Base class for pairing failures."""


class PairingInvalidError(PairingError):
    """Malformed, expired, revoked, reused, or badly signed material."""


class PairingNotFoundError(PairingError):
    """No such invitation, link, or pairing request is visible."""


class PairingConflictError(PairingError):
    """An open pairing already exists, or state forbids the action."""


class PairingTransportError(PairingError):
    """The pairing message could not be delivered over the Gateway."""
