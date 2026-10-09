"""Replay registry tests: primitives, expiry, concurrency, verify flow."""

import dataclasses
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backend.identity.errors import (
    ExpiredMessageError,
    IdentityError,
    InvalidSignatureError,
    ReplayDetectedError,
)
from backend.identity.identity import create_identity
from backend.identity.replay import ReplayRegistry
from backend.identity.signing import sign_message
from backend.identity.verification import verify_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.types import PROTOCOL_VERSION, MessageType

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)
EXPIRY = BASE + timedelta(hours=1)


def _signed(identity=None, message_id="msg_001"):
    identity = identity or create_identity()
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id="req_123",
        sender=identity.agent_id,
        recipient="agent:peer",
        type=MessageType.REMOTE_QUERY,
        created_at=BASE,
        expires_at=EXPIRY,
        payload=RemoteQueryPayload(query="hi", requested_capabilities=[]),
        signature="",
    )
    return identity, sign_message(message, identity.private_key)


def test_first_message_accepted():  # 38
    registry = ReplayRegistry()
    assert registry.check_and_record("msg_001", EXPIRY) is True
    assert registry.has_seen("msg_001", NOW) is True


def test_same_message_id_rejected_as_replay():  # 39
    registry = ReplayRegistry()
    identity, signed = _signed()
    verify_message(signed, identity.public_key, now=NOW,
                   replay_registry=registry)
    with pytest.raises(ReplayDetectedError):
        verify_message(signed, identity.public_key, now=NOW,
                       replay_registry=registry)
    assert isinstance(ReplayDetectedError("x"), IdentityError)


def test_different_message_id_with_same_request_id_accepted():  # 40
    registry = ReplayRegistry()
    identity, first = _signed(message_id="msg_001")
    _, second = _signed(identity, message_id="msg_002")
    assert first.request_id == second.request_id == "req_123"
    verify_message(first, identity.public_key, now=NOW,
                   replay_registry=registry)
    result = verify_message(second, identity.public_key, now=NOW,
                            replay_registry=registry)
    assert result.message_id == "msg_002"


def test_expired_replay_entry_expires_from_registry():  # 41
    registry = ReplayRegistry()
    assert registry.check_and_record("msg_old", BASE) is True
    assert registry.purge_expired(NOW) == 1
    assert registry.has_seen("msg_old", NOW) is False
    assert len(registry) == 0
    # check_and_record admits the id again once its window has passed
    assert registry.check_and_record("msg_old", EXPIRY) is True


def test_concurrent_check_and_record_single_winner():  # 42
    registry = ReplayRegistry()
    winners: list[bool] = []
    barrier = threading.Barrier(16)
    # Live window on the real clock: with the lazy stale-drop rule, a
    # long-past fixed expiry would admit every thread, which is correct
    # but not what this atomicity test measures.
    live = datetime.now(timezone.utc) + timedelta(hours=1)

    def attempt():
        barrier.wait()
        winners.append(registry.check_and_record("msg_race", live))

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert winners.count(True) == 1
    assert winners.count(False) == 15


def test_registry_rejects_invalid_input():
    registry = ReplayRegistry()
    with pytest.raises(ValueError):
        registry.record("not a valid id!!", EXPIRY)
    with pytest.raises(ValueError):
        registry.record("msg_001", datetime(2026, 1, 1))  # naive
    with pytest.raises(ValueError):
        registry.has_seen("msg_001", datetime(2026, 1, 1))  # naive now


def test_forgery_does_not_consume_message_id():
    registry = ReplayRegistry()
    identity, signed = _signed()
    forged = dataclasses.replace(
        signed,
        payload=RemoteQueryPayload(query="evil", requested_capabilities=[]),
    )
    with pytest.raises(InvalidSignatureError):
        verify_message(forged, identity.public_key, now=NOW,
                       replay_registry=registry)
    assert len(registry) == 0
    result = verify_message(signed, identity.public_key, now=NOW,
                            replay_registry=registry)
    assert result.message_id == "msg_001"


def test_replay_after_expiration_reports_expiry():
    registry = ReplayRegistry()
    identity, signed = _signed()
    verify_message(signed, identity.public_key, now=NOW,
                   replay_registry=registry)
    with pytest.raises(ExpiredMessageError):
        verify_message(signed, identity.public_key,
                       now=EXPIRY + timedelta(hours=1),
                       replay_registry=registry)


def test_check_and_record_drops_stale_entry():
    registry = ReplayRegistry()
    assert registry.check_and_record("msg_stale", EXPIRY) is True
    # Window passed without purge: stale entry is dropped, id re-admitted.
    assert registry.check_and_record(
        "msg_stale", EXPIRY + timedelta(hours=1), now=EXPIRY + timedelta(seconds=1)
    ) is True
    # Live entry still blocks duplicates.
    assert registry.check_and_record(
        "msg_stale", EXPIRY + timedelta(hours=2), now=EXPIRY + timedelta(seconds=1)
    ) is False
