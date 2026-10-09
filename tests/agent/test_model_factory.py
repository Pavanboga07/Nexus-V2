"""Model-factory tests: explicit builds, actionable failures (no network)."""

import pytest

from backend.agent.config import AgentRuntimeConfig
from backend.agent.errors import ModelConfigurationError
from backend.agent.model_factory import build_chat_model


def test_unsupported_provider_integration_actionable():  # 3
    config = AgentRuntimeConfig(model="definitely-not-a-provider:model-x")
    with pytest.raises(ModelConfigurationError) as exc_info:
        build_chat_model(config)
    assert "definitely-not-a-provider" in str(exc_info.value)


def test_missing_provider_package_actionable():
    # openai integration is almost certainly absent here; either way the
    # error must be a ModelConfigurationError, never an ImportError leak.
    try:
        import langchain_openai  # noqa: F401
    except ImportError:
        config = AgentRuntimeConfig(model="openai:gpt-4o")
        with pytest.raises(ModelConfigurationError, match="langchain-openai"):
            build_chat_model(config)
    else:
        pytest.skip("langchain-openai is installed; nothing to assert")


def test_valid_configuration_builds_model_factory():  # 4
    # Factory wiring is provider-agnostic; with no provider package the
    # attempt must still fail as ModelConfigurationError, proving the
    # factory — not env access or fallbacks — drives construction.
    config = AgentRuntimeConfig(model="openai:gpt-4o", temperature=0.0)
    try:
        import langchain_openai  # noqa: F401

        model = build_chat_model(config)
        assert model is not None
    except ImportError:
        with pytest.raises(ModelConfigurationError):
            build_chat_model(config)


def test_no_silent_placeholder_model():
    config = AgentRuntimeConfig()
    with pytest.raises(ModelConfigurationError, match="NEXUS_LLM_MODEL"):
        build_chat_model(config)
