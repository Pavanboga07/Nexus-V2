"""Chat-model construction (explicit config in, LangChain model out).

Real models are built with ``langchain.chat_models.init_chat_model``
from a ``provider:model-id`` reference. Provider integration packages
(e.g. ``langchain-openai``) stay optional: their absence surfaces as
an actionable :class:`ModelConfigurationError`, never as a silent
fallback. Only ``temperature``/``max_tokens`` set by configuration are
forwarded; timeouts are enforced by the runtime around invocation.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from .config import AgentRuntimeConfig
from .errors import ModelConfigurationError

_PROVIDER_PACKAGES = {
    "openai": "langchain-openai",
    "anthropic": "langchain-anthropic",
    "google_genai": "langchain-google-genai",
    "azure_openai": "langchain-openai",
    "bedrock": "langchain-aws",
    "vertexai": "langchain-google-vertexai",
    "mistralai": "langchain-mistralai",
    "groq": "langchain-groq",
    "ollama": "langchain-ollama",
}


def parse_model_ref(reference: str) -> tuple[str, str]:
    """Split ``provider:model-id`` (both parts required, non-empty)."""
    if not isinstance(reference, str) or ":" not in reference:
        raise ModelConfigurationError(
            "model must use provider-prefixed form 'provider:model-id'"
        )
    provider, _, model_id = reference.partition(":")
    if not provider.strip() or not model_id.strip():
        raise ModelConfigurationError(
            "model must use provider-prefixed form 'provider:model-id'"
        )
    return provider.strip(), model_id.strip()


def build_chat_model(config: AgentRuntimeConfig) -> BaseChatModel:
    """Construct the configured chat model (fail fast, fail clearly)."""
    if not config.model:
        raise ModelConfigurationError(
            "no model configured: set NEXUS_LLM_MODEL to "
            "'provider:model-id' (e.g. 'openai:gpt-4o')"
        )
    provider, model_id = parse_model_ref(config.model)
    try:
        from langchain.chat_models import init_chat_model
    except ImportError as exc:
        raise ModelConfigurationError(
            "the installed langchain package does not provide model "
            "initialization"
        ) from exc
    options: dict = {}
    if config.temperature is not None:
        options["temperature"] = config.temperature
    if config.max_output_tokens is not None:
        options["max_tokens"] = config.max_output_tokens
    try:
        model = init_chat_model(
            model_id, model_provider=provider, **options
        )
    except ImportError as exc:
        package = _PROVIDER_PACKAGES.get(provider, f"langchain-{provider}")
        raise ModelConfigurationError(
            f"provider integration missing for {provider!r}: "
            f"install {package!r} to use model {config.model!r}"
        ) from exc
    except Exception as exc:
        raise ModelConfigurationError(
            f"cannot initialize model {config.model!r}: {exc}"
        ) from exc
    if not isinstance(model, BaseChatModel):
        raise ModelConfigurationError(
            f"provider {provider!r} did not return a chat model"
        )
    return model
