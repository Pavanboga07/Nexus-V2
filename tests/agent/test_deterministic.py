"""Deterministic development model: explicit opt-in, normal interface."""

import pytest
from langchain_core.messages import HumanMessage

from backend.agent.config import AgentRuntimeConfig
from backend.agent.deterministic import DeterministicModel
from backend.agent.errors import ModelConfigurationError
from backend.agent.model_factory import build_chat_model


def test_deterministic_gated_off_by_default():
    config = AgentRuntimeConfig(model="deterministic:demo")
    assert config.allow_deterministic is False
    with pytest.raises(ModelConfigurationError) as exc_info:
        build_chat_model(config)
    assert "NEXUS_LLM_ALLOW_DETERMINISTIC" in str(exc_info.value)


def test_deterministic_opt_in_from_env():
    config = AgentRuntimeConfig.from_env(
        environ={
            "NEXUS_LLM_MODEL": "deterministic:demo",
            "NEXUS_LLM_ALLOW_DETERMINISTIC": "1",
        }
    )
    assert config.allow_deterministic is True
    model = build_chat_model(config)
    assert isinstance(model, DeterministicModel)


def test_deterministic_answers_through_normal_interface():
    model = DeterministicModel(label="demo")
    result = model._generate([HumanMessage(content="  Hello\nworld  ")])
    text = result.generations[0].message.content
    assert text == "[deterministic:demo] Hello world"


def test_deterministic_empty_input_still_labeled():
    model = DeterministicModel(label="demo")
    result = model._generate([])
    assert result.generations[0].message.content == (
        "[deterministic:demo] (no input)"
    )


def test_real_providers_unchanged_without_credentials():
    # A real provider reference without integration packages must still
    # fail as ModelConfigurationError — the deterministic path adds no
    # fallback for production references.
    config = AgentRuntimeConfig(
        model="openai:gpt-4o", allow_deterministic=True
    )
    try:
        import langchain_openai  # noqa: F401
    except ImportError:
        with pytest.raises(ModelConfigurationError):
            build_chat_model(config)
    else:
        pytest.skip("langchain-openai is installed; nothing to assert")
