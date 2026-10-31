from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from debate_agent.config import AgentProfile, RunConfig, _default_profiles
from debate_agent.models import AgentName


def test_catalog_registers_every_builtin_agent_with_exact_metadata() -> None:
    from debate_agent.agent_catalog import (
        AGENT_CATALOG,
        AgentDefinition,
        ModelPolicy,
    )

    assert set(AGENT_CATALOG) == set(AgentName)
    assert set(ModelPolicy) == {
        ModelPolicy.DISCOVER_AND_FREEZE,
        ModelPolicy.CLI_DEFAULT,
        ModelPolicy.EXPLICIT,
    }
    assert tuple(AGENT_CATALOG.values()) == (
        AgentDefinition(
            name=AgentName.CODEX,
            display_name="Codex",
            default_command="codex",
            model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
            supports_variant=False,
            supports_mcp=True,
            ui_color="#275dad",
        ),
        AgentDefinition(
            name=AgentName.CLAUDE,
            display_name="Claude",
            default_command="claude",
            model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
            supports_variant=False,
            supports_mcp=True,
            ui_color="#cf5b4e",
        ),
        AgentDefinition(
            name=AgentName.QODER,
            display_name="Qoder",
            default_command="qodercli",
            model_policy=ModelPolicy.CLI_DEFAULT,
            supports_variant=False,
            supports_mcp=True,
            ui_color="#26836f",
        ),
        AgentDefinition(
            name=AgentName.OPENCODE,
            display_name="OpenCode",
            default_command="opencode",
            model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
            supports_variant=True,
            freezes_runtime_config=True,
            supports_mcp=True,
            ui_color="gray",
        ),
        AgentDefinition(
            name=AgentName.OPENCLAW,
            display_name="OpenClaw",
            default_command="openclaw",
            model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
            supports_variant=True,
            variant_label="Thinking",
            supports_native_profile=True,
            freezes_runtime_config=True,
            supports_mcp=True,
            ui_color="gray",
        ),
        AgentDefinition(
            name=AgentName.HERMES,
            display_name="Hermes",
            default_command="hermes",
            model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
            supports_provider=True,
            freezes_runtime_config=True,
            experimental=True,
            supports_mcp=True,
            ui_color="gray",
        ),
        AgentDefinition(
            name=AgentName.CODEBUDDY,
            display_name="CodeBuddy",
            default_command="codebuddy",
            model_policy=ModelPolicy.CLI_DEFAULT,
            supports_native_profile=True,
            freezes_runtime_config=True,
            experimental=True,
            supports_mcp=True,
            ui_color="#6b4eff",
        ),
    )
    for definition in (
        AGENT_CATALOG[AgentName.CODEX],
        AGENT_CATALOG[AgentName.CLAUDE],
        AGENT_CATALOG[AgentName.QODER],
        AGENT_CATALOG[AgentName.OPENCODE],
    ):
        assert definition.variant_label is None
        assert definition.supports_native_profile is False
        assert definition.supports_provider is False
        assert definition.experimental is False


def test_catalog_queries_use_stable_order_and_reject_unknown_agent() -> None:
    from debate_agent.agent_catalog import (
        agent_definition,
        agent_definitions,
        default_command,
    )

    assert tuple(definition.name for definition in agent_definitions()) == (
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
        AgentName.OPENCODE,
        AgentName.OPENCLAW,
        AgentName.HERMES,
        AgentName.CODEBUDDY,
    )
    assert agent_definition(AgentName.CLAUDE).display_name == "Claude"
    assert default_command(AgentName.QODER) == "qodercli"
    with pytest.raises(KeyError, match="未知 Agent"):
        agent_definition(cast(AgentName, "unknown"))


@pytest.mark.parametrize(
    "agent",
    ("codex", "claude", "qoder", "opencode", "openclaw", "hermes", "codebuddy"),
)
def test_catalog_queries_reject_agent_name_strings(agent: str) -> None:
    from debate_agent.agent_catalog import agent_definition, default_command

    string_agent = cast(AgentName, agent)
    with pytest.raises(KeyError, match="未知 Agent"):
        agent_definition(string_agent)
    with pytest.raises(KeyError, match="未知 Agent"):
        default_command(string_agent)


def test_default_profiles_remain_identical_to_pre_catalog_baseline() -> None:
    assert _default_profiles() == {
        AgentName.CODEX: AgentProfile(command="codex"),
        AgentName.CLAUDE: AgentProfile(command="claude"),
        AgentName.QODER: AgentProfile(command="qodercli"),
        AgentName.OPENCODE: AgentProfile(command="opencode"),
        AgentName.OPENCLAW: AgentProfile(command="openclaw"),
        AgentName.HERMES: AgentProfile(command="hermes"),
        AgentName.CODEBUDDY: AgentProfile(command="codebuddy"),
    }


def test_run_config_migrates_historical_participants_without_catalog_changes(
    tmp_path: Path,
) -> None:
    config = RunConfig.model_validate(
        {
            "workspace": tmp_path,
            "topic": "历史配置",
            "profiles": {
                "codex": {"command": "codex"},
                "claude": {"command": "claude"},
            },
        }
    )

    assert config.participants == (AgentName.CODEX, AgentName.CLAUDE)
    assert config.first_agent is AgentName.CODEX


def test_agent_profile_variant_is_optional_and_historical_profiles_still_load(
    tmp_path: Path,
) -> None:
    profile = AgentProfile(command="opencode", variant="high")
    historical = RunConfig.model_validate(
        {
            "workspace": tmp_path,
            "topic": "历史配置",
            "profiles": {
                "codex": {"command": "codex"},
                "claude": {"command": "claude"},
            },
        }
    )

    assert profile.variant == "high"
    assert AgentProfile(command="codex").variant is None
    assert historical.participants == (AgentName.CODEX, AgentName.CLAUDE)
    assert AgentName.OPENCODE not in historical.profiles


def test_agent_profile_freezes_native_profile_and_provider_in_run_config(
    tmp_path: Path,
) -> None:
    """验证厂商 Profile/Provider 进入冻结配置；参数为临时目录，无返回值。"""
    profile = AgentProfile(
        command="future-cli",
        native_profile="debate",
        provider="custom",
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="冻结厂商配置",
        profiles={
            AgentName.CODEX: profile,
            AgentName.CLAUDE: AgentProfile(command="claude"),
        },
    )

    restored = RunConfig.model_validate_json(config.model_dump_json())

    assert restored.profiles[AgentName.CODEX].native_profile == "debate"
    assert restored.profiles[AgentName.CODEX].provider == "custom"
    assert AgentProfile(command="codex").native_profile is None
    assert AgentProfile(command="codex").provider is None


def test_run_config_only_requires_opencode_profile_when_selected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="opencode"):
        RunConfig(
            workspace=tmp_path,
            topic="选择 OpenCode",
            participants=(AgentName.CODEX, AgentName.OPENCODE),
            profiles={AgentName.CODEX: AgentProfile(command="codex")},
        )
