"""HTTP error mapping: typed domain failures become safe responses."""

import pytest

from backend.agent.errors import (
    ConversationNotFoundError,
    InvalidInputError,
    ModelInvocationError,
)
from backend.api.errors import ApiError, to_api_error
from backend.execution.errors import (
    ApprovalMissingError,
    ExecutionConflictError,
    ExecutionError,
)
from backend.requests.errors import (
    ApprovalError,
    InvalidStateTransitionError,
    PeerError,
    RequestConflictError,
    RequestValidationError,
    ResponseCorrelationError,
    RetryableInfrastructureError,
    UnknownAgentError,
    UnknownRequestError,
)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RequestValidationError("x"), 422, "INVALID_INPUT"),
        (InvalidInputError("x"), 422, "INVALID_INPUT"),
        (ApprovalError("x"), 422, "APPROVAL_REJECTED"),
        (PeerError("x"), 422, "PEER_UNAVAILABLE"),
        (ResponseCorrelationError("x"), 422, "RESPONSE_REJECTED"),
        (ExecutionError("x"), 422, "EXECUTION_FAILED"),
        (UnknownAgentError("x"), 404, "UNKNOWN_AGENT"),
        (UnknownRequestError("x"), 404, "UNKNOWN_REQUEST"),
        (ConversationNotFoundError("x"), 404, "UNKNOWN_CONVERSATION"),
        (InvalidStateTransitionError("x"), 409, "ILLEGAL_TRANSITION"),
        (RequestConflictError("x"), 409, "ALREADY_EXISTS"),
        (ApprovalMissingError("x"), 409, "APPROVAL_MISSING"),
        (ExecutionConflictError("x"), 409, "EXECUTION_CONFLICT"),
        (RetryableInfrastructureError("x"), 503, "UPSTREAM_UNAVAILABLE"),
        (ModelInvocationError("x"), 503, "MODEL_UNAVAILABLE"),
    ],
)
def test_domain_errors_map_to_safe_triples(error, status, code):
    mapped = to_api_error(error)
    assert isinstance(mapped, ApiError)
    assert mapped.status == status
    assert mapped.code == code
    # Static safe text only: never the exception's message.
    assert mapped.message != str(error) or str(error) in ("x",)


def test_unknown_errors_become_generic_500():
    mapped = to_api_error(RuntimeError("db exploded: secret"))
    assert mapped.status == 500
    assert mapped.code == "INTERNAL_ERROR"
    assert "secret" not in mapped.message
    assert "exploded" not in mapped.message


def test_raw_operational_errors_are_retryable_503():
    from sqlalchemy.exc import OperationalError

    mapped = to_api_error(OperationalError("select 1", {}, None))
    assert mapped.status == 503
    assert mapped.code == "UPSTREAM_UNAVAILABLE"


def test_forbidden_and_unauthorized_triples():
    from backend.api.errors import (
        AUTH_NOT_CONFIGURED,
        EXECUTION_NOT_CONFIGURED,
        FORBIDDEN,
        MODEL_NOT_CONFIGURED,
        SIGNING_NOT_CONFIGURED,
        UNAUTHORIZED,
    )

    assert UNAUTHORIZED[0] == 401
    assert FORBIDDEN[0] == 403
    for triple in (AUTH_NOT_CONFIGURED, MODEL_NOT_CONFIGURED,
                   EXECUTION_NOT_CONFIGURED, SIGNING_NOT_CONFIGURED):
        assert triple[0] == 503
