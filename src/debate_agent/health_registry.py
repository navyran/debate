"""内置 CLI 健康策略注册表与模型发现入口。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from debate_agent.health_codebuddy import CodeBuddyCliHealthStrategy
from debate_agent.health_hermes import HermesCliHealthStrategy
from debate_agent.health_models import CliHealthRegistryError, CliHealthStrategy
from debate_agent.health_opencode import OpenCodeHealthStrategy
from debate_agent.health_simple_strategies import (
    ClaudeCliHealthStrategy,
    CodexCliHealthStrategy,
    OpenClawCliHealthStrategy,
    QoderCliHealthStrategy,
)
from debate_agent.models import AgentName

HEALTH_STRATEGIES: dict[AgentName, CliHealthStrategy] = {
    AgentName.CODEX: CodexCliHealthStrategy(),
    AgentName.CLAUDE: ClaudeCliHealthStrategy(),
    AgentName.QODER: QoderCliHealthStrategy(),
    AgentName.OPENCODE: OpenCodeHealthStrategy(),
    AgentName.OPENCLAW: OpenClawCliHealthStrategy(),
    AgentName.HERMES: HermesCliHealthStrategy(),
    AgentName.CODEBUDDY: CodeBuddyCliHealthStrategy(),
}


def health_strategy(agent: AgentName) -> CliHealthStrategy:
    """严格读取 CLI 健康策略。

    参数为 AgentName；返回静态注册策略；字符串、未知值或缺失注册项抛配置错误。
    """
    if not isinstance(agent, AgentName):
        raise CliHealthRegistryError(f"健康策略未注册: {agent}")
    try:
        return HEALTH_STRATEGIES[agent]
    except KeyError as exc:
        raise CliHealthRegistryError(f"健康策略未注册: {agent.value}") from exc


def discover_cli_model(
    agent: AgentName,
    *,
    home: Path,
    environment: Mapping[str, str],
) -> str | None:
    """发现本机显式模型。

    参数为 Agent、HOME 和环境；返回模型名或 None；损坏配置被安全忽略。
    """
    return health_strategy(agent).discover_model(
        home=home,
        environment=environment,
    )
