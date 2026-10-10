"""Component 10/11 contract: executor output verifies on receipt.

Runs the real Component 10 executor and proves its persisted
REMOTE_RESPONSE passes exactly the checks the originating-side
response path relies on: structural validation plus Ed25519
verification against the responder's stored public key, with the
request/response binding intact. (Full originator completion lives
in tests/responses/, where the row follows the SENT lifecycle; here
the executor itself terminally completes the shared test row.)
"""

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from backend.identity.keys import public_key_to_bytes
from backend.identity.verification import verify_message
from backend.persistence.database import session_scope
from backend.persistence.repositories.messages import MessageRepository
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.protocol.validation import validate_message

from .conftest import (
    AnswerModel,
    Keys,
    make_executor,
    make_runtime,
    seed_approved,
)


@pytest.mark.asyncio
async def test_executor_response_verifies_for_receiver(db):
    keys = Keys()
    sender, recipient, request_id = await seed_approved(db, keys)
    outcome = await make_executor(
        db, keys, recipient.agent_id,
        make_runtime(AnswerModel(text="four")),
    ).execute(request_id)
    assert outcome.status == "COMPLETED"

    async with session_scope(db.sessions) as session:
        stored = await MessageRepository(session).get(
            outcome.response_message_id)
    assert stored is not None
    wire = {**stored.envelope, "signature": stored.signature}
    message = message_from_dict(wire)
    # The receiver's trust core, byte for byte.
    assert validate_message(message) == []
    assert verify_message(
        message,
        Ed25519PublicKey.from_public_bytes(
            public_key_to_bytes(recipient.public_key)),
    ) is not None
    assert message.request_id == request_id
    assert message.sender == recipient.agent_id
    assert message.recipient == sender.agent_id
    assert message.payload.status == "ok"
    assert message.payload.answer == "four"
    # The stored bytes round-trip unchanged for the worker path.
    assert message_to_dict(message) == wire
