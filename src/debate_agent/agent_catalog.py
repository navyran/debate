"""内置 CLI Agent 的纯元数据目录。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from debate_agent.models import AgentName


class ModelPolicy(str, Enum):
    """表示 Agent 的模型选择策略。"""

    DISCOVER_AND_FREEZE = "discover_and_freeze"
    CLI_DEFAULT = "cli_default"
    EXPLICIT = "explicit"


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """描述受信任内置 CLI 的静态元数据。"""

    name: AgentName
    display_name: str
    default_command: str
    model_policy: ModelPolicy
    supports_variant: bool = False
    variant_label: str | None = None
    supports_native_profile: bool = False
    supports_provider: bool = False
    freezes_runtime_config: bool = False
    experimental: bool = False
    supports_mcp: bool = True
    ui_color: str = "gray"


AGENT_CATALOG: dict[AgentName, AgentDefinition] = {
    AgentName.CODEX: AgentDefinition(
        name=AgentName.CODEX,
        display_name="Codex",
        default_command="codex",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
        ui_color="#275dad",
    ),
    AgentName.CLAUDE: AgentDefinition(
        name=AgentName.CLAUDE,
        display_name="Claude",
        default_command="claude",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
        ui_color="#cf5b4e",
    ),
    AgentName.QODER: AgentDefinition(
        name=AgentName.QODER,
        display_name="Qoder",
        default_command="qodercli",
        model_policy=ModelPolicy.CLI_DEFAULT,
        ui_color="#26836f",
    ),
    AgentName.OPENCODE: AgentDefinition(
        name=AgentName.OPENCODE,
        display_name="OpenCode",
        default_command="opencode",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
        supports_variant=True,
        freezes_runtime_config=True,
    ),
    AgentName.OPENCLAW: AgentDefinition(
        name=AgentName.OPENCLAW,
        display_name="OpenClaw",
        default_command="openclaw",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
        supports_variant=True,
        variant_label="Thinking",
        supports_native_profile=True,
        freezes_runtime_config=True,
    ),
    AgentName.HERMES: AgentDefinition(
        name=AgentName.HERMES,
        display_name="Hermes",
        default_command="hermes",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
        supports_provider=True,
        freezes_runtime_config=True,
        experimental=True,
    ),
    AgentName.CODEBUDDY: AgentDefinition(
        name=AgentName.CODEBUDDY,
        display_name="CodeBuddy",
        default_command="codebuddy",
        model_policy=ModelPolicy.CLI_DEFAULT,
        supports_native_profile=True,
        freezes_runtime_config=True,
        experimental=True,
        ui_color="#6b4eff",
    ),
}


def agent_definitions() -> tuple[AgentDefinition, ...]:
    """按固定注册顺序返回内置 Agent 定义。

    参数：无。
    返回值：按 Codex、Claude、Qoder、OpenCode、OpenClaw、Hermes、CodeBuddy
    顺序排列的元组。
    异常：不抛出异常。
    """
    return tuple(AGENT_CATALOG.values())


def agent_definition(agent: AgentName) -> AgentDefinition:
    """读取指定内置 Agent 的定义。

    参数：agent 为需要查询的 Agent 名称。
    返回值：对应的静态 Agent 定义。
    异常：参数不是 AgentName 或 Agent 未注册时抛出 KeyError。
    """
    if not isinstance(agent, AgentName):
        raise KeyError(f"未知 Agent: {agent}")
    try:
        return AGENT_CATALOG[agent]
    except KeyError as exc:
        raise KeyError(f"未知 Agent: {agent}") from exc


def default_command(agent: AgentName) -> str:
    """读取指定内置 Agent 的默认 CLI 命令。

    参数：agent 为需要查询的 Agent 名称。
    返回值：对应的默认命令文本。
    异常：未注册 Agent 时抛出 KeyError。
    """
    return agent_definition(agent).default_command
