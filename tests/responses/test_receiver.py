"""Response receiver tests: correlation, idempotency, hostility (real PG).

The originating side creates a request, the worker hands it off
(QUEUED -> SENT with outbox SENT), and a signed REMOTE_RESPONSE later
arrives from the remote agent. Every assertion below reads persisted
PostgreSQL state: the receiver keeps no memory that matters.
"""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from backend.identity.identity import create_identity
from backend.persistence.database import session_scope
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.requests.errors import ResponseCorrelationError
from backend.requests.service import RequestService
from backend.responses import (
    DUPLICATE,
    REGISTERED,
    REJECTED,
    ResponseReceiverError,
)

from .conftest import (
    Keys,
    _pub,
    live_times,
    make_receiver,
    response_messages,
    seed_originator_sent,
    signed_response,
    unique,
)


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def _request(db, request_id):
    service = RequestService(db.sessions, _ReadKeys())
    return await service.get_request(request_id)


@pytest.mark.asyncio
async def test_valid_success_completes_through_receiver(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(response, now=created)
    assert outcome.status == REGISTERED
    assert outcome.request_id == request_id
    assert outcome.response_message_id == response.message_id
    assert outcome.detail == "COMPLETED"
    assert outcome.record is not None
    assert outcome.record.status == "COMPLETED"
    stored = await _request(db, request_id)
    assert stored.status == "COMPLETED"
    assert stored.response_message_id == response.message_id
    rows = await response_messages(db, request_id)
    assert len(rows) == 1
    assert rows[0].message_id == response.message_id
    rebuilt = message_from_dict(
        {**rows[0].envelope, "signature": rows[0].signature})
    assert rebuilt.payload.answer == "four"


@pytest.mark.asyncio
async def test_linked_thread_receives_verified_answer_once(db):
    from backend.persistence.database import session_scope as _scope
    from backend.persistence.repositories.agents import (
        AgentRepository as _Agents,
    )
    from backend.persistence.repositories.conversations import (
        ConversationMessageRepository,
        ConversationRepository,
    )

    async def _chat_rows():
        async with _scope(db.sessions) as session:
            return await ConversationMessageRepository(
                session).list_by_conversation(thread.id)

    keys = Keys()
    originator = keys.add(create_identity())
    remote = keys.add(create_identity())
    async with _scope(db.sessions) as session:
        await _Agents(session).create(
            originator.agent_id, _pub(originator))
        thread = await ConversationRepository(session).create(
            originator.agent_id, title="t")
    originator, remote, request_id = await seed_originator_sent(
        db, keys, conversation_id=thread.id,
        originator=originator, remote=remote)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(response, now=created)
    assert outcome.status == REGISTERED
    rows = await _chat_rows()
    assert len(rows) == 1
    assert rows[0].role == "agent"
    assert rows[0].content == "four"
    assert rows[0].request_id == request_id
    # Redelivery resolves DUPLICATE without a second chat message.
    again = await make_receiver(
        db, keys, originator.agent_id).handle(response, now=created)
    assert again.status == DUPLICATE
    assert len(await _chat_rows()) == 1


@pytest.mark.asyncio
async def test_failed_or_unlinked_responses_leave_chat_alone(db):
    from backend.persistence.database import session_scope as _scope
    from backend.persistence.repositories.agents import (
        AgentRepository as _Agents,
    )
    from backend.persistence.repositories.conversations import (
        ConversationMessageRepository,
        ConversationRepository,
    )

    async def _chat_count(thread_id):
        async with _scope(db.sessions) as session:
            return len(await ConversationMessageRepository(
                session).list_by_conversation(thread_id))

    keys = Keys()
    originator = keys.add(create_identity())
    remote = keys.add(create_identity())
    async with _scope(db.sessions) as session:
        await _Agents(session).create(
            originator.agent_id, _pub(originator))
        thread = await ConversationRepository(session).create(
            originator.agent_id, title="t")
    _, _, failed_id = await seed_originator_sent(
        db, keys, conversation_id=thread.id,
        originator=originator, remote=remote)
    created, _ = live_times()
    failure = signed_response(
        remote, originator.agent_id, failed_id, created=created,
        payload_over={"status": "error", "answer": "",
                      "error_code": "EXECUTION_FAILED",
                      "error_message": "remote execution failed"},
    )
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(failure, now=created)
    assert outcome.status == REGISTERED
    assert outcome.detail == "FAILED"
    assert await _chat_count(thread.id) == 0
    # Unlinked requests never touch any thread either.
    o2, r2, plain_id = await seed_originator_sent(db, keys)
    created2, _ = live_times()
    plain = signed_response(
        r2, o2.agent_id, plain_id, created=created2)
    outcome2 = await make_receiver(
        db, keys, o2.agent_id).handle(plain, now=created2)
    assert outcome2.status == REGISTERED
    assert await _chat_count(thread.id) == 0


@pytest.mark.asyncio
async def test_error_response_fails_request(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created,
        payload_over={"status": "error", "answer": "",
                      "error_code": "EXECUTION_FAILED",
                      "error_message": "remote execution failed"},
    )
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(response, now=created)
    assert outcome.status == REGISTERED
    assert outcome.detail == "FAILED"
    stored = await _request(db, request_id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "EXECUTION_FAILED"
    assert stored.failure_message == "remote execution failed"
    assert stored.response_message_id == response.message_id


@pytest.mark.asyncio
async def test_duplicate_redelivery_returns_duplicate(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    receiver = make_receiver(db, keys, originator.agent_id)
    first = await receiver.handle(response, now=created)
    assert first.status == REGISTERED
    second = await receiver.handle(copy.deepcopy(response), now=created)
    assert second.status == DUPLICATE
    assert second.detail == "already-processed"
    assert second.response_message_id == response.message_id
    assert second.record is not None
    assert second.record.status == "COMPLETED"
    stored = await _request(db, request_id)
    assert stored.status == "COMPLETED"
    assert len(await response_messages(db, request_id)) == 1


@pytest.mark.asyncio
async def test_conflicting_message_id_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    first = signed_response(
        remote, originator.agent_id, request_id, created=created)
    receiver = make_receiver(db, keys, originator.agent_id)
    assert (await receiver.handle(first, now=created)).status == REGISTERED
    # Same request, fresh message id, different answer: only the first
    # response ever counts; the request already rests terminal.
    forged = signed_response(
        remote, originator.agent_id, request_id, created=created,
        payload_over={"answer": "five"},
    )
    assert forged.message_id != first.message_id
    outcome = await receiver.handle(forged, now=created)
    assert outcome.status == REJECTED
    assert outcome.response_message_id is None
    stored = await _request(db, request_id)
    assert stored.status == "COMPLETED"
    assert stored.response_message_id == first.message_id
    rows = await response_messages(db, request_id)
    assert [row.message_id for row in rows] == [first.message_id]


@pytest.mark.asyncio
async def test_wrong_request_id_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, unique("req"), created=created)
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(response, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, request_id)).status == "SENT"
    assert await response_messages(db, request_id) == []


@pytest.mark.asyncio
async def test_unexpected_sender_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    stranger = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        from backend.persistence.repositories.agents import AgentRepository
        from .conftest import _pub

        await AgentRepository(session).create(
            stranger.agent_id, _pub(stranger))
    created, _ = live_times()
    response = signed_response(
        stranger, originator.agent_id, request_id, created=created)
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(response, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, request_id)).status == "SENT"
    assert await response_messages(db, request_id) == []


@pytest.mark.asyncio
async def test_wrong_recipient_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)

    other = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        from backend.persistence.repositories.agents import AgentRepository
        from .conftest import _pub

        await AgentRepository(session).create(other.agent_id, _pub(other))
    created, _ = live_times()
    response = signed_response(
        remote, other.agent_id, request_id, created=created)
    outcome = await make_receiver(
        db, keys, other.agent_id).handle(response, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, request_id)).status == "SENT"


@pytest.mark.asyncio
async def test_invalid_signature_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    tampered = message_to_dict(message_from_dict(message_to_dict(response)))
    tampered["payload"] = dict(tampered["payload"])
    tampered["payload"]["answer"] = "forged"
    forged = message_from_dict(tampered)
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(forged, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, request_id)).status == "SENT"
    assert await response_messages(db, request_id) == []


@pytest.mark.asyncio
async def test_expired_response_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    response = signed_response(
        remote, originator.agent_id, request_id,
        created=created, expires=created + timedelta(minutes=30),
    )
    outcome = await make_receiver(db, keys, originator.agent_id).handle(
        response)
    assert outcome.status == REJECTED
    assert (await _request(db, request_id)).status == "SENT"
    assert await response_messages(db, request_id) == []


@pytest.mark.asyncio
async def test_mismatched_payload_type_rejected(db):
    from backend.protocol.models import NexusMessage, RemoteQueryPayload
    from backend.protocol.types import PROTOCOL_VERSION, MessageType

    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, expires = live_times()
    # A REMOTE_RESPONSE carrying a query payload: structurally
    # unparseable at the service boundary (never signed — parse fails
    # before any cryptographic work).
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=unique("msg"),
        request_id=request_id,
        sender=remote.agent_id,
        recipient=originator.agent_id,
        type=MessageType.REMOTE_RESPONSE,
        created_at=created,
        expires_at=expires,
        payload=RemoteQueryPayload(
            query="wrong shape", requested_capabilities=[]),
        signature="",
    )
    outcome = await make_receiver(
        db, keys, originator.agent_id).handle(message, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, request_id)).status == "SENT"
    assert await response_messages(db, request_id) == []


@pytest.mark.asyncio
async def test_response_before_dispatch_rejected(db):
    keys = Keys()

    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        from backend.persistence.repositories.agents import AgentRepository
        from backend.persistence.repositories.peers import PeerRepository
        from .conftest import _pub

        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    created, expires = live_times()
    service = RequestService(db.sessions, keys)
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "early?", [], expires,
        now=created,
    )
    assert record.status == "QUEUED"
    response = signed_response(
        recipient, sender.agent_id, record.request_id, created=created)
    outcome = await make_receiver(
        db, keys, sender.agent_id).handle(response, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, record.request_id)).status == "QUEUED"
    assert await response_messages(db, record.request_id) == []


@pytest.mark.asyncio
async def test_response_after_terminal_rejected(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    first = signed_response(
        remote, originator.agent_id, request_id, created=created,
        payload_over={"status": "error", "answer": "",
                      "error_code": "EXECUTION_FAILED",
                      "error_message": "boom"},
    )
    receiver = make_receiver(db, keys, originator.agent_id)
    assert (await receiver.handle(first, now=created)).status == REGISTERED
    assert (await _request(db, request_id)).status == "FAILED"
    late = signed_response(
        remote, originator.agent_id, request_id, created=created)
    outcome = await receiver.handle(late, now=created)
    assert outcome.status == REJECTED
    stored = await _request(db, request_id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_reused_message_id_for_other_request_rejected(db):
    from backend.requests.service import RequestService

    keys = Keys()
    originator, remote, request_a = await seed_originator_sent(db, keys)
    created0, expires0 = live_times()
    service = RequestService(db.sessions, keys)
    extra = await service.create_remote_request(
        originator.agent_id, remote.agent_id, "second?", [],
        expires0, now=created0,
    )
    request_b = (await service.mark_sent(extra.request_id)).request_id
    created, _ = live_times()
    first = signed_response(
        remote, originator.agent_id, request_a, created=created)
    receiver = make_receiver(db, keys, originator.agent_id)
    assert (await receiver.handle(first, now=created)).status == REGISTERED
    # Same physical id rebound to another request (validly signed by
    # the same remote): the id is consumed, so this is a conflict,
    # not a duplicate of anything for request B.
    rebound = signed_response(
        remote, originator.agent_id, request_b, created=created,
        message_id=first.message_id,
    )
    outcome = await receiver.handle(rebound, now=created)
    assert outcome.status == REJECTED
    assert (await _request(db, request_b)).status == "SENT"
    assert await response_messages(db, request_b) == []
    assert (await _request(db, request_a)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_concurrent_duplicates_have_one_winner(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    receiver = make_receiver(db, keys, originator.agent_id)

    async def attempt():
        return await receiver.handle(
            copy.deepcopy(response), now=created)

    outcomes = await asyncio.gather(attempt(), attempt())
    assert sorted(o.status for o in outcomes) == [DUPLICATE, REGISTERED]
    assert (await _request(db, request_id)).status == "COMPLETED"
    assert len(await response_messages(db, request_id)) == 1


@pytest.mark.asyncio
async def test_persistence_failure_leaves_no_trace(db):
    from backend.persistence.repositories import messages as messages_mod

    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    receiver = make_receiver(db, keys, originator.agent_id)
    with mock.patch.object(
        messages_mod.MessageRepository, "create",
        side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            await receiver.handle(response, now=created)
    assert (await _request(db, request_id)).status == "SENT"
    assert await response_messages(db, request_id) == []
    # The retry succeeds once persistence recovers: nothing partial.
    retry = await receiver.handle(response, now=created)
    assert retry.status == REGISTERED
    assert (await _request(db, request_id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_recreated_receiver_classifies_identically(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    first = make_receiver(db, keys, originator.agent_id)
    assert (await first.handle(response, now=created)).status == REGISTERED
    second = make_receiver(db, keys, originator.agent_id)
    again = await second.handle(copy.deepcopy(response), now=created)
    assert again.status == DUPLICATE
    assert (await _request(db, request_id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_non_response_message_rejected_as_misuse(db):
    from backend.protocol.models import RemoteQueryPayload
    from backend.protocol.types import MessageType

    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    created, _ = live_times()
    query = signed_response(
        remote, originator.agent_id, request_id, created=created)
    import dataclasses

    not_response = dataclasses.replace(
        query, type=MessageType.REMOTE_QUERY,
        payload=RemoteQueryPayload(
            query="q", requested_capabilities=[]),
    )
    receiver = make_receiver(db, keys, originator.agent_id)
    with pytest.raises(ResponseReceiverError):
        await receiver.handle(not_response, now=created)
    with pytest.raises(ResponseReceiverError):
        await receiver.handle(object(), now=created)
    assert (await _request(db, request_id)).status == "SENT"
