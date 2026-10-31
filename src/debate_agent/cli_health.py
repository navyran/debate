"""CLI 健康检查、厂商策略和恢复预检的兼容门面。"""

from debate_agent.circuit_breaker import CircuitBreakerStore, CircuitRecord
from debate_agent.health_checker import CliHealthChecker
from debate_agent.health_codebuddy import CodeBuddyCliHealthStrategy
from debate_agent.health_hermes import HermesCliHealthStrategy
from debate_agent.health_models import (
    CLI_DEFAULT_MODEL,
    OPENCODE_MCP_LIST_MAX_BYTES,
    OPENCODE_MCP_MAX_COUNT,
    OPENCODE_RESOLVED_CONFIG_MAX_BYTES,
    CliCommandResult,
    CliHealthCheck,
    CliHealthRegistryError,
    CliHealthReport,
    CliHealthStrategy,
    CliHealthStrategyContext,
    CliHealthStrategyResult,
    CliIdentity,
    CliPreflightError,
    CommandExecutor,
    FailureCategory,
    FailureClassifier,
    GitProbeInitializer,
    HealthCommandRunner,
    ManagedCliHealthStrategy,
    SubprocessCommandExecutor,
    filter_cli_environment,
)
from debate_agent.health_opencode import OpenCodeHealthStrategy
from debate_agent.health_preflight import (
    freeze_run_config,
    preflight_run_config,
    rebuild_codebuddy_resume_config,
    rebuild_hermes_resume_config,
    rebuild_openclaw_resume_config,
    rebuild_opencode_resume_config,
    rebuild_resume_config,
)
from debate_agent.health_registry import (
    HEALTH_STRATEGIES,
    discover_cli_model,
    health_strategy,
)
from debate_agent.health_simple_strategies import (
    ClaudeCliHealthStrategy,
    CodexCliHealthStrategy,
    OpenClawCliHealthStrategy,
    QoderCliHealthStrategy,
)

__all__ = [
    "CLI_DEFAULT_MODEL",
    "HEALTH_STRATEGIES",
    "OPENCODE_MCP_LIST_MAX_BYTES",
    "OPENCODE_MCP_MAX_COUNT",
    "OPENCODE_RESOLVED_CONFIG_MAX_BYTES",
    "CircuitBreakerStore",
    "CircuitRecord",
    "ClaudeCliHealthStrategy",
    "CliCommandResult",
    "CliHealthCheck",
    "CliHealthChecker",
    "CliHealthRegistryError",
    "CliHealthReport",
    "CliHealthStrategy",
    "CliHealthStrategyContext",
    "CliHealthStrategyResult",
    "CliIdentity",
    "CliPreflightError",
    "CodeBuddyCliHealthStrategy",
    "CodexCliHealthStrategy",
    "CommandExecutor",
    "FailureCategory",
    "FailureClassifier",
    "GitProbeInitializer",
    "HealthCommandRunner",
    "HermesCliHealthStrategy",
    "ManagedCliHealthStrategy",
    "OpenClawCliHealthStrategy",
    "OpenCodeHealthStrategy",
    "QoderCliHealthStrategy",
    "SubprocessCommandExecutor",
    "discover_cli_model",
    "filter_cli_environment",
    "freeze_run_config",
    "health_strategy",
    "preflight_run_config",
    "rebuild_codebuddy_resume_config",
    "rebuild_hermes_resume_config",
    "rebuild_openclaw_resume_config",
    "rebuild_opencode_resume_config",
    "rebuild_resume_config",
]
