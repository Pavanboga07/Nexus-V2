"""Nexus Identity & Cryptographic Security (Component 2).

Ed25519 agent identities, message signing/verification over Component 1
canonical bytes, and in-memory replay primitives. Depends on
``backend.protocol``; the protocol package does not depend back
(Protocol -> Identity only).

No authentication, authorization, persistence, transport, gateway,
LangGraph/LangChain, or MCP code lives here.
"""

from .errors import (
    ExpiredMessageError,
    IdentityError,
    InvalidIdentityError,
    InvalidPrivateKeyError,
    InvalidPublicKeyError,
    InvalidSignatureError,
    MalformedSignatureError,
    ReplayDetectedError,
    SenderMismatchError,
)
from .identity import (
    create_identity,
    derive_agent_id,
    identity_from_private_bytes,
    identity_from_private_key,
    public_identity_from_dict,
    verify_agent_identity,
)
from .keys import (
    generate_private_key,
    private_key_from_bytes,
    private_key_to_bytes,
    public_key_from_bytes,
    public_key_to_bytes,
)
from .models import (
    AGENT_ID_PREFIX,
    KEY_ALGORITHM,
    PRIVATE_KEY_LENGTH,
    PUBLIC_KEY_LENGTH,
    AgentIdentity,
    PublicIdentity,
)
from .replay import ReplayRegistry
from .signing import (
    SIGNATURE_LENGTH,
    decode_signature,
    encode_signature,
    sign_message,
)
from .verification import VerificationResult, verify_message

__all__ = [
    "IdentityError",
    "InvalidIdentityError",
    "InvalidPrivateKeyError",
    "InvalidPublicKeyError",
    "MalformedSignatureError",
    "InvalidSignatureError",
    "ExpiredMessageError",
    "ReplayDetectedError",
    "SenderMismatchError",
    "AgentIdentity",
    "PublicIdentity",
    "KEY_ALGORITHM",
    "AGENT_ID_PREFIX",
    "PRIVATE_KEY_LENGTH",
    "PUBLIC_KEY_LENGTH",
    "SIGNATURE_LENGTH",
    "generate_private_key",
    "private_key_to_bytes",
    "private_key_from_bytes",
    "public_key_to_bytes",
    "public_key_from_bytes",
    "create_identity",
    "identity_from_private_bytes",
    "identity_from_private_key",
    "derive_agent_id",
    "verify_agent_identity",
    "public_identity_from_dict",
    "encode_signature",
    "decode_signature",
    "sign_message",
    "VerificationResult",
    "verify_message",
    "ReplayRegistry",
]
