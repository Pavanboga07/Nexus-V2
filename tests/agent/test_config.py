"""Configuration tests: explicit settings, validated limits (no I/O)."""

import pytest

from backend.agent.config import AgentRuntimeConfig
from backend.agent.errors import InvalidConfigurationError, ModelConfigurationError


def test_missing_model_configuration_fails_clearly():  # 1
    config = AgentRuntimeConfig()
    assert config.model is None
    with pytest.raises(ModelConfigurationError, match="NEXUS_LLM_MODEL"):
        from backend.agent.model_factory import build_chat_model

        build_chat_model(config)


def test_malformed_configuration_fails_clearly():  # 2
    with pytest.raises(ModelConfigurationError):
        from backend.agent.model_factory import parse_model_ref

        parse_model_ref("no-provider-prefix")
    with pytest.raises(ModelConfigurationError):
        from backend.agent.model_factory import parse_model_ref

        parse_model_ref(":empty-provider")
    with pytest.raises(ModelConfigurationError):
        from backend.agent.model_factory import parse_model_ref

        parse_model_ref("provider:")
    assert parse_model_ref("openai:gpt-4o") == ("openai", "gpt-4o")


def test_configuration_limits_are_validated():  # 6
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig(max_input_chars=0)
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig(max_history_messages=-1)
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig(request_timeout_s=0)
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig(max_output_tokens=0)
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig(temperature=99.0)
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig(system_instruction="   ")
    valid = AgentRuntimeConfig(
        model="openai:gpt-4o", temperature=0.5, max_output_tokens=256)
    assert valid.temperature == 0.5


def test_from_env_parsing():
    config = AgentRuntimeConfig.from_env(environ={
        "NEXUS_LLM_MODEL": "openai:gpt-4o",
        "NEXUS_LLM_TEMPERATURE": "0.2",
        "NEXUS_LLM_MAX_TOKENS": "512",
        "NEXUS_LLM_TIMEOUT_S": "30",
        "NEXUS_LLM_MAX_INPUT_CHARS": "1000",
        "NEXUS_LLM_MAX_HISTORY": "5",
    })
    assert config.model == "openai:gpt-4o"
    assert config.temperature == 0.2
    assert config.max_output_tokens == 512
    assert config.request_timeout_s == 30.0
    assert config.max_input_chars == 1000
    assert config.max_history_messages == 5
    empty = AgentRuntimeConfig.from_env(environ={})
    assert empty.model is None
    assert empty.temperature is None
    with pytest.raises(InvalidConfigurationError):
        AgentRuntimeConfig.from_env(environ={"NEXUS_LLM_MAX_TOKENS": "lots"})
