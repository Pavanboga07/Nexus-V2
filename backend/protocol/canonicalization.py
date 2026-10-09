"""Nexus Protocol canonicalization.

Defines the deterministic byte representation of a message that Component 2
(Ed25519 signing) will sign and verify. This module performs no
cryptography: it only produces stable bytes.

Canonical form:
  - JSON object with keys sorted lexicographically (``sort_keys=True``),
    including nested payload keys;
  - no insignificant whitespace (``separators=(",", ":")``);
  - UTF-8 encoding (``ensure_ascii=False``);
  - timestamps normalized to UTC ISO-8601 (``+00:00``);
  - the ``signature`` field is EXCLUDED (it is the signature *over* this
    content, so including it would be circular).

What is canonicalized: every envelope field except ``signature``
(``protocol_version``, ``message_id``, ``request_id``, ``sender``,
``recipient``, ``type``, ``created_at``, ``expires_at``, ``payload``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import NexusMessage
from .serialization import (
    ProtocolSerializationError,
    format_timestamp,
    message_to_dict,
    parse_timestamp,
)


def _normalize_timestamp(value: Any) -> str:
    """Normalize a timestamp value to canonical UTC ISO-8601 text."""
    from datetime import datetime

    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, str):
        return format_timestamp(parse_timestamp(value))
    raise ProtocolSerializationError(
        [f"invalid timestamp in canonical form: {value!r}"]
    )


def message_to_canonical_dict(
    message: NexusMessage | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the canonical dict covered by future signatures.

    Accepts a typed message or an equivalent raw mapping. The ``signature``
    key is dropped when present; timestamps are normalized so equivalent
    instants written in different ISO-8601 spellings canonicalize equally.
    """
    if isinstance(message, NexusMessage):
        data = message_to_dict(message)
    elif isinstance(message, Mapping):
        data = dict(message)
    else:
        raise ProtocolSerializationError(
            [f"cannot canonicalize {type(message).__name__}"]
        )
    data.pop("signature", None)
    if "created_at" in data:
        data["created_at"] = _normalize_timestamp(data["created_at"])
    if "expires_at" in data:
        data["expires_at"] = _normalize_timestamp(data["expires_at"])
    return data


def canonicalize_message(
    message: NexusMessage | Mapping[str, Any],
) -> bytes:
    """Return deterministic canonical bytes for ``message``.

    The same logical message always yields identical bytes, regardless of
    Python dict insertion order or JSON whitespace in the input. JSON
    encoding failures surface as :class:`ProtocolSerializationError`;
    raw ``TypeError``/``ValueError``/``UnicodeEncodeError`` never escape
    this function (only the ``json.dumps``/``encode`` calls are wrapped,
    so unrelated programming errors elsewhere still propagate).
    """
    canonical = message_to_canonical_dict(message)
    try:
        text = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolSerializationError(
            [f"message is not JSON serializable: {exc}"]
        ) from exc
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolSerializationError(
            [f"message is not UTF-8 encodable: {exc}"]
        ) from exc
