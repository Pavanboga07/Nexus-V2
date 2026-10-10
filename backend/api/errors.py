"""Component 12 error model: HTTP mapping for typed domain failures.

Every entry maps to a static ``(status, code, message)`` triple —
never exception text, queries, keys, signatures, or traces. The
mapping is total over the service errors the routes can raise; anything
unlisted becomes a generic 500 without detail.
"""

from __future__ import annotations


class ApiError(Exception):
    """An HTTP failure with a static code and safe message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# Authentication / configuration (raised before any service call).
UNAUTHORIZED = (401, "UNAUTHORIZED", "authentication required")
AUTH_NOT_CONFIGURED = (
    503,
    "AUTH_NOT_CONFIGURED",
    "API authentication is not configured on this deployment",
)
FORBIDDEN = (403, "FORBIDDEN", "not authorized for this resource")
MODEL_NOT_CONFIGURED = (
    503,
    "MODEL_NOT_CONFIGURED",
    "no language model is configured on this deployment",
)
EXECUTION_NOT_CONFIGURED = (
    503,
    "EXECUTION_NOT_CONFIGURED",
    "remote execution is not configured on this deployment",
)
SIGNING_NOT_CONFIGURED = (
    503,
    "SIGNING_NOT_CONFIGURED",
    "request signing is not configured on this deployment",
)

#: Exact-type map: domain error -> (status, code, safe message).
_HTTP_MAP: dict[type, tuple[int, str, str]] = {}


def _register(error_type: type, mapped: tuple[int, str, str]) -> None:
    _HTTP_MAP[error_type] = mapped


def _build() -> None:
    from backend.agent.errors import (
        AgentNotAvailableError,
        ConversationMismatchError,
        ConversationNotFoundError,
        InvalidInputError as AgentInvalidInput,
        InvalidModelOutputError,
        ModelInvocationError,
    )
    from backend.execution.errors import (
        ApprovalMissingError,
        ExecutionConflictError,
        ExecutionError,
        ExecutionInfrastructureError,
        NotInboundRequestError,
        RequestExpiredError as ExecutionExpired,
        RequestNotApprovedError,
    )
    from backend.inbound.errors import InboundError
    from backend.requests.errors import (
        ApprovalError,
        InactiveAgentError,
        InvalidStateTransitionError,
        PeerError,
        RequestConflictError,
        RequestValidationError,
        ResponseCorrelationError,
        RetryableInfrastructureError,
        UnknownAgentError,
        UnknownRequestError,
    )
    from backend.pairing.errors import (
        PairingConflictError,
        PairingInvalidError,
        PairingNotFoundError,
        PairingTransportError,
    )
    from backend.provider_config.errors import (
        CredentialNotConfigured,
        CredentialStoreUnavailable,
        ProviderAuthError,
        ProviderCallError,
        ProviderConfigInvalid,
        ProviderUnavailableError,
    )
    from backend.responses.errors import ResponseReceiverError

    validation = (422, "INVALID_INPUT", "request input is invalid")
    for error in (
        RequestValidationError,
        AgentInvalidInput,
        InboundError,
        ResponseReceiverError,
        ProviderConfigInvalid,
    ):
        _register(error, validation)
    _register(ApprovalError, (422, "APPROVAL_REJECTED",
                              "approval decision cannot be recorded"))
    _register(PeerError, (422, "PEER_UNAVAILABLE",
                          "no connected peer for this direction"))
    _register(ResponseCorrelationError, (422, "RESPONSE_REJECTED",
                                        "response cannot be correlated"))
    _register(InvalidModelOutputError, (502, "MODEL_BAD_OUTPUT",
                                       "model returned no usable response"))
    _register(UnknownAgentError, (404, "UNKNOWN_AGENT", "agent not found"))
    _register(UnknownRequestError, (404, "UNKNOWN_REQUEST",
                                   "request not found"))
    _register(ConversationNotFoundError, (404, "UNKNOWN_CONVERSATION",
                                         "conversation not found"))
    _register(ConversationMismatchError, (404, "UNKNOWN_CONVERSATION",
                                         "conversation not found"))
    _register(AgentNotAvailableError, (404, "AGENT_UNAVAILABLE",
                                      "agent is unknown or inactive"))
    _register(InvalidStateTransitionError, (409, "ILLEGAL_TRANSITION",
                                           "operation not allowed "
                                           "in the current state"))
    _register(RequestConflictError, (409, "ALREADY_EXISTS",
                                    "identifier already exists"))
    _register(RequestNotApprovedError, (409, "NOT_APPROVED",
                                       "request is not approved "
                                       "for execution"))
    _register(ApprovalMissingError, (409, "APPROVAL_MISSING",
                                    "no recipient approval recorded"))
    _register(NotInboundRequestError, (403, "NOT_INBOUND",
                                      "request is not inbound work "
                                      "for this agent"))
    _register(ExecutionExpired, (409, "REQUEST_EXPIRED",
                                "request has expired"))
    _register(ExecutionConflictError, (409, "EXECUTION_CONFLICT",
                                      "request left its expected state"))
    _register(ExecutionError, (422, "EXECUTION_FAILED",
                              "remote execution failed"))
    _register(CredentialStoreUnavailable, (503, "CREDENTIAL_STORE_UNAVAILABLE",
        "provider credential storage is not configured on this deployment"))
    _register(CredentialNotConfigured, (404, "PROVIDER_NOT_CONFIGURED",
                                      "no provider configuration is saved"))
    _register(ProviderAuthError, (401, "PROVIDER_UNAUTHORIZED",
                                 "the provider rejected the credentials"))
    _register(ProviderUnavailableError, (503, "PROVIDER_UNAVAILABLE",
                                       "the provider could not be reached"))
    _register(ProviderCallError, (502, "PROVIDER_BAD_RESPONSE",
                                 "the provider returned no usable response"))
    _register(PairingInvalidError, (422, "PAIRING_REJECTED",
                                      "the pairing material is invalid"))
    _register(PairingNotFoundError, (404, "PAIRING_NOT_FOUND",
                                    "pairing invitation or link not found"))
    _register(PairingConflictError, (409, "PAIRING_CONFLICT",
                                    "pairing state forbids this action"))
    _register(PairingTransportError, (503, "PAIRING_TRANSPORT",
                                     "the pairing message could not be "
                                     "delivered; it is safe to retry"))
    _register(ModelInvocationError, (503, "MODEL_UNAVAILABLE",
                                    "model invocation failed"))
    retryable = (503, "UPSTREAM_UNAVAILABLE",
                 "transient infrastructure failure")
    for error in (RetryableInfrastructureError, ExecutionInfrastructureError):
        _register(error, retryable)


_build()


def to_api_error(exc: BaseException) -> ApiError:
    """Map a domain failure to its HTTP triple (exact-type lookup)."""
    mapped = _HTTP_MAP.get(type(exc))
    if mapped is not None:
        status, code, message = mapped
        return ApiError(status, code, message)
    # Untranslated connectivity failures (paths without the service
    # decorator) are retryable by the same project convention.
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.exc import TimeoutError as SQLTimeoutError

    if isinstance(exc, (OperationalError, SQLTimeoutError, OSError)):
        return ApiError(503, "UPSTREAM_UNAVAILABLE",
                        "transient infrastructure failure")
    return ApiError(500, "INTERNAL_ERROR", "internal error")
