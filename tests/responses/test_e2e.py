"""End-to-end originator loop: real Gateway, clients, worker, receiver.

Alice creates an outbound request; the real outbox worker hands it to
a live Gateway server over a real GatewayClient (QUEUED -> SENT with
outbox SENT). Bob's live dispatcher registers the query. Bob answers
with a signed REMOTE_RESPONSE over his own live client; Alice's live
dispatcher — with the response receiver attached — completes the
request. No mocks on the transport path, no sleeps for timeouts.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.gateway_client.client import GatewayClient
from backend.identity.identity import create_identity
from backend.inbound import InboundDispatcher, InboundProcessor
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.protocol.models import NexusMessage, RemoteResponsePayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.requests.service import RequestService
from backend.responses import ResponseReceiver
from backend.worker.config import WorkerConfig
from backend.worker.outbox_dispatcher import OutboxDispatcher
from gateway.app import GatewayServer, utcnow

from .conftest import Keys, _pub, live_times, unique


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def _request(db, request_id):
    return await RequestService(db.sessions, _ReadKeys()).get_request(
        request_id)


@pytest.mark.asyncio
async def test_originator_loop_over_live_gateway(db):
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    url = f"ws://127.0.0.1:{server.port}"
    try:
        keys = Keys()
        alice = keys.add(create_identity())
        bob = keys.add(create_identity())
        identities = {alice.agent_id: alice, bob.agent_id: bob}
        async with session_scope(db.sessions) as session:
            await AgentRepository(session).create(
                alice.agent_id, _pub(alice))
            await AgentRepository(session).create(bob.agent_id, _pub(bob))
            await PeerRepository(session).create(
                alice.agent_id, bob.agent_id, status="connected")
            await PeerRepository(session).create(
                bob.agent_id, alice.agent_id, status="connected")
        created, expires = live_times()
        service = RequestService(db.sessions, keys)
        record = await service.create_remote_request(
            alice.agent_id, bob.agent_id, "What is two plus two?", [],
            expires, now=created,
        )
        request_id = record.request_id

        client_a = GatewayClient(alice, url)
        client_b = GatewayClient(bob, url)
        await client_a.connect()
        await client_b.connect()
        try:
            proc_b = InboundProcessor(db.sessions, bob.agent_id)
            disp_b, recv_b = InboundDispatcher.from_client(client_b, proc_b)
            task_b = disp_b.start(recv_b)
            proc_a = InboundProcessor(db.sessions, alice.agent_id)
            disp_a, recv_a = InboundDispatcher.from_client(client_a, proc_a)
            receiver = ResponseReceiver(db.sessions, alice.agent_id, keys)
            await receiver.attach(disp_a)
            task_a = disp_a.start(recv_a)
            try:
                # The worker shares alice's live client for her own
                # identity (a second pooled connection would replace
                # hers at the gateway and strand this test's reader).
                worker = OutboxDispatcher(
                    sessions=db.sessions,
                    identities=keys,
                    gateway_url=url,
                    service=RequestService(db.sessions, keys),
                    config=WorkerConfig(),
                    client_factory=(
                        lambda sid: client_a
                        if sid == alice.agent_id
                        else GatewayClient(identities[sid], url)
                    ),
                    clock=lambda: datetime.now(timezone.utc),
                    jitter=lambda: 0.0,
                )
                try:
                    results = await worker.run_once(batch_size=100)
                finally:
                    await worker.close()
                # Closing the worker also closed the shared client:
                # reconnect it before the answer arrives.
                await client_a.ensure_connected()
                ours = [r for r in results if r.request_id == request_id]
                assert len(ours) == 1
                assert ours[0].outcome.value == "SENT"
                assert (await _request(db, request_id)).status == "SENT"

                # The originator row is SENT (its own lifecycle) and
                # stays there until the verified response arrives: on
                # this shared test database bob's processor meets the
                # existing row as a duplicate and changes nothing.
                assert (await _request(db, request_id)).status == "SENT"

                # Bob answers over his live client.
                now = utcnow()
                answer = NexusMessage(
                    protocol_version=PROTOCOL_VERSION,
                    message_id=unique("msg"),
                    request_id=request_id,
                    sender=bob.agent_id,
                    recipient=alice.agent_id,
                    type=MessageType.REMOTE_RESPONSE,
                    created_at=now,
                    expires_at=now + timedelta(minutes=30),
                    payload=RemoteResponsePayload(
                        status="ok", answer="four"),
                    signature="",
                )
                from backend.identity.signing import sign_message

                envelope = message_to_dict(
                    sign_message(answer, bob.private_key))
                ack = await client_b.send_message(envelope)
                assert ack.status == "DELIVERED"
                assert ack.message_id == envelope["message_id"]

                for _ in range(200):
                    done = await _request(db, request_id)
                    if done is not None and done.status == "COMPLETED":
                        break
                    await asyncio.sleep(0.05)
                assert done is not None
                assert done.status == "COMPLETED"
                assert done.response_message_id == envelope["message_id"]
            finally:
                await disp_a.stop()
                await disp_b.stop()
                assert task_a.done() and task_b.done()
        finally:
            await client_a.close()
            await client_b.close()
    finally:
        await server.stop()
