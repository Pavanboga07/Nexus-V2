"""Inbound queue-ownership regression tests (real lifecycle, real PG).

Proves at most one active dispatcher consumes a given client's queue:
competitors fail clearly, ownership releases on stop/cancel/crash, and
independent clients are unaffected.
"""

import asyncio

import pytest

from backend.identity.identity import create_identity
from backend.inbound import (
    DispatcherAlreadyRunning,
    InboundDispatcher,
    InboundProcessor,
)
from backend.requests.service import RequestService

from .conftest import (
    live_query,
    seed_recipient_side,
    unique,
)


class _Keys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


class _FakeClient:
    """Minimal inbound source with an ownable queue slot."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def receive_inbound(self, timeout=None):
        if timeout is None:
            return await self.incoming.get()
        return await asyncio.wait_for(self.incoming.get(), timeout)


async def _registered_processor(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    return sender, recipient, InboundProcessor(db.sessions, recipient.agent_id)


@pytest.mark.asyncio
async def test_second_dispatcher_cannot_consume_same_client(db):
    sender, recipient, processor = await _registered_processor(db)
    client = _FakeClient()
    first, recv_first = InboundDispatcher.from_client(client, processor)
    second, _ = InboundDispatcher.from_client(
        client, InboundProcessor(processor._sessions, recipient.agent_id)
    )
    task = first.start(recv_first)
    try:
        assert first.running
        with pytest.raises(DispatcherAlreadyRunning):
            second.start(client.receive_inbound)
        assert not second.running
        # The owner still routes; the competitor never sees a message.
        message = live_query(
            sender, recipient, request_id=unique("req"),
            message_id=unique("msg"),
        )
        await client.incoming.put(message)
        service = RequestService(db.sessions, _Keys())
        for _ in range(100):
            seen = await service.get_request(message.request_id)
            if seen is not None:
                break
            await asyncio.sleep(0.05)
        assert seen is not None
        assert seen.status == "WAITING_APPROVAL"
        assert second.unhandled.qsize() == 0
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.asyncio
async def test_separate_clients_consume_independently(db):
    sender, recipient, processor = await _registered_processor(db)
    first_client, second_client = _FakeClient(), _FakeClient()
    first, recv_first = InboundDispatcher.from_client(
        first_client, processor
    )
    second, recv_second = InboundDispatcher.from_client(
        second_client,
        InboundProcessor(processor._sessions, recipient.agent_id),
    )
    first_task = first.start(recv_first)
    second_task = second.start(recv_second)
    try:
        assert first.running and second.running
        service = RequestService(db.sessions, _Keys())
        sent = []
        for client in (first_client, second_client):
            message = live_query(
                sender, recipient, request_id=unique("req"),
                message_id=unique("msg"),
            )
            sent.append(message)
            await client.incoming.put(message)
        for message in sent:
            for _ in range(100):
                seen = await service.get_request(message.request_id)
                if seen is not None:
                    break
                await asyncio.sleep(0.05)
            assert seen is not None
            assert seen.status == "WAITING_APPROVAL"
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.asyncio
async def test_stop_releases_ownership_for_replacement(db):
    _, recipient, processor = await _registered_processor(db)
    client = _FakeClient()
    first, recv_first = InboundDispatcher.from_client(client, processor)
    task = first.start(recv_first)
    assert first.running
    await first.stop()
    assert not first.running
    assert task.done()
    second, recv_second = InboundDispatcher.from_client(
        client, InboundProcessor(processor._sessions, recipient.agent_id)
    )
    replacement = second.start(recv_second)
    try:
        assert second.running
    finally:
        await second.stop()
        assert replacement.done()


@pytest.mark.asyncio
async def test_cancellation_releases_without_stop(db):
    _, recipient, processor = await _registered_processor(db)
    client = _FakeClient()
    first, recv_first = InboundDispatcher.from_client(client, processor)
    task = first.start(recv_first)
    assert first.running
    # Abrupt end with no stop(): ownership must still be freed.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)  # let task-done callbacks settle
    second, recv_second = InboundDispatcher.from_client(
        client, InboundProcessor(processor._sessions, recipient.agent_id)
    )
    replacement = second.start(recv_second)
    try:
        assert second.running
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_consumer_crash_releases_for_replacement(db):
    sender, recipient, processor = await _registered_processor(db)
    client = _FakeClient()
    first, recv_first = InboundDispatcher.from_client(client, processor)

    async def poisoned():
        raise RuntimeError("source died")

    task = first.start(poisoned)
    try:
        await task
        assert False, "consumer should have failed"
    except RuntimeError:
        pass
    assert not first.running
    second, recv_second = InboundDispatcher.from_client(
        client, InboundProcessor(processor._sessions, recipient.agent_id)
    )
    message = live_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    await client.incoming.put(message)
    replacement = second.start(recv_second)
    try:
        service = RequestService(db.sessions, _Keys())
        for _ in range(100):
            seen = await service.get_request(message.request_id)
            if seen is not None:
                break
            await asyncio.sleep(0.05)
        assert seen is not None
        assert seen.status == "WAITING_APPROVAL"
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_concurrent_startup_has_single_winner(db):
    _, recipient, processor = await _registered_processor(db)
    client = _FakeClient()
    first, _ = InboundDispatcher.from_client(client, processor)
    second, _ = InboundDispatcher.from_client(
        client, InboundProcessor(processor._sessions, recipient.agent_id)
    )

    async def attempt(dispatcher):
        await asyncio.sleep(0)
        return dispatcher.start(client.receive_inbound)

    winners, loser_errors = [], []
    results = await asyncio.gather(
        attempt(first), attempt(second), return_exceptions=True
    )
    for dispatcher, result in zip((first, second), results):
        if isinstance(result, asyncio.Task):
            winners.append(dispatcher)
        else:
            loser_errors.append(result)
    try:
        assert len(winners) == 1
        assert len(loser_errors) == 1
        assert isinstance(loser_errors[0], DispatcherAlreadyRunning)
        assert sum(d.running for d in (first, second)) == 1
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.asyncio
async def test_same_dispatcher_restart_still_works(db):
    _, recipient, processor = await _registered_processor(db)
    client = _FakeClient()
    dispatcher, receive = InboundDispatcher.from_client(client, processor)
    first_task = dispatcher.start(receive)
    await dispatcher.stop()
    assert first_task.done()
    second_task = dispatcher.start(receive)
    try:
        assert dispatcher.running
        assert second_task is not first_task
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_real_gateway_client_carries_owner_slot(db):
    from backend.gateway_client import GatewayClient

    _, recipient, processor = await _registered_processor(db)
    identity = create_identity()
    client = GatewayClient(identity, "ws://127.0.0.1:1/")
    assert client._inbound_owner is None
    first, receive = InboundDispatcher.from_client(client, processor)
    task = first.start(receive)
    try:
        assert client._inbound_owner is first
        second, _ = InboundDispatcher.from_client(
            client, InboundProcessor(processor._sessions, recipient.agent_id)
        )
        with pytest.raises(DispatcherAlreadyRunning):
            second.start(client.receive_inbound)
        await second.stop()
    finally:
        await first.stop()
    assert client._inbound_owner is None
