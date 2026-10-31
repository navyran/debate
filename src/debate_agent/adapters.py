"""内置 CLI Adapter 的兼容门面与注册表。"""

from __future__ import annotations

from debate_agent.adapter_claude import ClaudeAdapter, ClaudeStreamParser
from debate_agent.adapter_codebuddy import CodeBuddyAdapter, CodeBuddyStreamParser
from debate_agent.adapter_codex import CodexAdapter, CodexStreamParser
from debate_agent.adapter_core import (
    AGENT_ENVIRONMENT_PASSTHROUGH,
    ENVIRONMENT_OVERRIDE_ALLOWLIST,
    SENSITIVE_AGENT_ENVIRONMENT_KEYS,
    SENSITIVE_ENVIRONMENT_OVERRIDE_KEYS,
    AdapterContext,
    AdapterFactory,
    AdapterOutputError,
    AdapterRegistryError,
    AdapterStreamEvent,
    AgentAdapter,
    AgentStreamParser,
    Invocation,
    SessionBoundaryError,
    VendorTerminal,
)
from debate_agent.adapter_hermes import (
    HERMES_ARGV_PROMPT_MAX_BYTES,
    HermesAdapter,
    HermesStreamParser,
)
from debate_agent.adapter_openclaw import (
    OpenClawAdapter,
    OpenClawStreamParser,
    sanitize_openclaw_stdout,
)
from debate_agent.adapter_opencode import (
    OpenCodeAdapter,
    OpenCodeJsonlState,
    OpenCodeStreamParser,
    scan_opencode_jsonl,
)
from debate_agent.adapter_qoder import QoderAdapter, QoderStreamParser
from debate_agent.config import AgentProfile
from debate_agent.models import AgentName
from debate_agent.policy import PolicyViolationError

__all__ = [
    "ADAPTER_FACTORIES",
    "AGENT_ENVIRONMENT_PASSTHROUGH",
    "ENVIRONMENT_OVERRIDE_ALLOWLIST",
    "HERMES_ARGV_PROMPT_MAX_BYTES",
    "SENSITIVE_AGENT_ENVIRONMENT_KEYS",
    "SENSITIVE_ENVIRONMENT_OVERRIDE_KEYS",
    "AdapterContext",
    "AdapterFactory",
    "AdapterOutputError",
    "AdapterRegistryError",
    "AdapterStreamEvent",
    "AgentAdapter",
    "AgentStreamParser",
    "ClaudeAdapter",
    "ClaudeStreamParser",
    "CodeBuddyAdapter",
    "CodeBuddyStreamParser",
    "CodexAdapter",
    "CodexStreamParser",
    "HermesAdapter",
    "HermesStreamParser",
    "Invocation",
    "OpenClawAdapter",
    "OpenClawStreamParser",
    "OpenCodeAdapter",
    "OpenCodeJsonlState",
    "OpenCodeStreamParser",
    "QoderAdapter",
    "QoderStreamParser",
    "SessionBoundaryError",
    "VendorTerminal",
    "adapter_factory",
    "create_claude_adapter",
    "create_codebuddy_adapter",
    "create_codex_adapter",
    "create_hermes_adapter",
    "create_openclaw_adapter",
    "create_opencode_adapter",
    "create_qoder_adapter",
    "sanitize_openclaw_stdout",
    "scan_opencode_jsonl",
]


def create_codex_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 Codex Adapter。

    参数为冻结 Profile 和 Adapter 上下文；返回 Codex Adapter；Schema 无效时抛错。
    """
    return CodexAdapter(
        profile,
        schema_path=context.schema_path,
        resume_sandbox_verified=context.codex_resume_sandbox_verified,
    )


def create_claude_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 Claude Adapter。

    参数为冻结 Profile 和 Adapter 上下文；返回 Claude Adapter；
    工具或 Schema 无效时抛错。
    """
    return ClaudeAdapter(
        profile,
        schema_path=context.schema_path,
        allowed_tools=context.command_policy.claude_allowed_tools(),
        minimal_mcp=context.mcp_mode == "minimal",
    )


def create_qoder_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 Qoder Adapter。

    参数为冻结 Profile 和 Adapter 上下文；返回 yolo Qoder Adapter；Schema 无效时抛错。
    """
    return QoderAdapter(
        profile,
        schema_path=context.schema_path,
        minimal_mcp=context.mcp_mode == "minimal",
        yolo=True,
    )


def create_opencode_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 OpenCode Adapter。

    参数为冻结 Profile 和 Adapter 上下文；返回 OpenCode Adapter；未启用完全授权模式
    时抛 PolicyViolationError，Schema 无效时传播异常。
    """
    if not context.unrestricted_commands:
        raise PolicyViolationError("OpenCode Adapter 要求完全授权模式")
    return OpenCodeAdapter(profile, schema_path=context.schema_path)


def create_openclaw_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 OpenClaw Adapter。

    参数：profile 为冻结配置，context 为运行上下文。
    返回值：完全授权模式下的 OpenClawAdapter。
    异常：未启用完全授权模式时抛 PolicyViolationError。
    """
    if not context.unrestricted_commands:
        raise PolicyViolationError("OpenClaw Adapter 要求完全授权模式")
    return OpenClawAdapter(profile, schema_path=context.schema_path)


def create_hermes_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 Hermes Adapter。

    参数：profile 为冻结配置，context 为运行上下文。
    返回值：完全授权模式下的实验性 HermesAdapter。
    异常：未完全授权时抛 PolicyViolationError。
    """
    if not context.unrestricted_commands:
        raise PolicyViolationError("Hermes Adapter 要求完全授权模式")
    return HermesAdapter(profile, schema_path=context.schema_path)


def create_codebuddy_adapter(
    profile: AgentProfile, context: AdapterContext
) -> AgentAdapter:
    """创建 CodeBuddy Adapter。

    参数为冻结 Profile 和 Adapter 上下文；返回完全授权的 CodeBuddyAdapter；
    未启用完全授权模式时抛 PolicyViolationError。
    """
    if not context.unrestricted_commands:
        raise PolicyViolationError("CodeBuddy Adapter 要求完全授权模式")
    return CodeBuddyAdapter(
        profile,
        schema_path=context.schema_path,
        minimal_mcp=context.mcp_mode == "minimal",
    )


ADAPTER_FACTORIES: dict[AgentName, AdapterFactory] = {
    AgentName.CODEX: create_codex_adapter,
    AgentName.CLAUDE: create_claude_adapter,
    AgentName.QODER: create_qoder_adapter,
    AgentName.OPENCODE: create_opencode_adapter,
    AgentName.OPENCLAW: create_openclaw_adapter,
    AgentName.HERMES: create_hermes_adapter,
    AgentName.CODEBUDDY: create_codebuddy_adapter,
}


def adapter_factory(agent: AgentName) -> AdapterFactory:
    """严格读取 Adapter factory。

    参数为 AgentName；返回静态注册的 factory；字符串、未知值或缺失注册项抛配置错误。
    """
    if not isinstance(agent, AgentName):
        raise AdapterRegistryError(f"Adapter 未注册: {agent}")
    try:
        return ADAPTER_FACTORIES[agent]
    except KeyError as exc:
        raise AdapterRegistryError(f"Adapter 未注册: {agent.value}") from exc
