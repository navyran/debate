"""通用 CLI 健康检查执行器。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from debate_agent.adapters import (
    AGENT_ENVIRONMENT_PASSTHROUGH,
    SENSITIVE_AGENT_ENVIRONMENT_KEYS,
)
from debate_agent.agent_catalog import agent_definition
from debate_agent.circuit_breaker import CircuitBreakerStore
from debate_agent.config import (
    AgentProfile,
    _seal_runtime_config_capability,
)
from debate_agent.health_models import (
    CliCommandResult,
    CliHealthCheck,
    CliHealthReport,
    CliHealthStrategyContext,
    CliIdentity,
    CommandExecutor,
    FailureCategory,
    FailureClassifier,
    ManagedCliHealthStrategy,
    SubprocessCommandExecutor,
    filter_cli_environment,
)
from debate_agent.health_registry import health_strategy
from debate_agent.invocation_events import Redactor
from debate_agent.models import AgentName


class CliHealthChecker:
    """按固定顺序验证 CLI 路径、版本、登录、模型连接和 MCP。"""

    base_environment_names = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")

    def __init__(
        self,
        *,
        executor: CommandExecutor | None = None,
        which: Callable[[str], str | None] = shutil.which,
        environment: Mapping[str, str] = os.environ,
        probe_root: Path,
        probe_timeout_seconds: float = 30,
    ) -> None:
        """创建预检器。

        参数为执行器、命令解析器、源环境、探针目录和时限；返回实例；时限非法时抛错。
        """
        if probe_timeout_seconds <= 0:
            raise ValueError("probe_timeout_seconds 必须为正数")
        self.executor = executor or SubprocessCommandExecutor()
        self.which = which
        self.environment = environment
        self.probe_root = probe_root.expanduser().resolve()
        self.probe_timeout_seconds = probe_timeout_seconds

    @staticmethod
    def _version(text: str) -> str | None:
        """提取语义版本；参数为版本输出，返回数字版本或 None，不抛异常。"""
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", text)
        return match.group(1) if match else None

    @staticmethod
    def _initialize_git_repository(path: Path) -> bool:
        """初始化临时 Git 探针库；参数为目录；返回是否成功；不回显命令输出。"""
        result = subprocess.run(
            ("git", "init", "--quiet"),
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def check(
        self,
        *,
        agent: AgentName,
        profile: AgentProfile,
        workspace: Path,
        mcp_mode: str,
        proxy_environment_allowlist: Sequence[str],
        circuit_breaker: CircuitBreakerStore | None = None,
    ) -> CliHealthReport:
        """执行一个 CLI 的只读预检。

        参数为 Agent/Profile/工作区/MCP 模式/环境名；返回报告；不获取仓库锁或写租约。
        """
        strategy = health_strategy(agent)
        required_features = strategy.required_features()
        checks: list[CliHealthCheck] = []
        evidence: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = []
        filtered_env = filter_cli_environment(
            self.environment,
            base_names=self.base_environment_names,
            proxy_names=proxy_environment_allowlist,
        )
        for name in AGENT_ENVIRONMENT_PASSTHROUGH[agent]:
            value = self.environment.get(name)
            if value:
                filtered_env[name] = value
        if (
            agent is AgentName.HERMES
            and profile.provider == "deepseek"
            and self.environment.get("DEEPSEEK_API_KEY")
        ):
            filtered_env["DEEPSEEK_API_KEY"] = self.environment["DEEPSEEK_API_KEY"]
        if isinstance(strategy, ManagedCliHealthStrategy):
            filtered_env = dict(
                strategy.initial_environment(base=filtered_env, mcp_mode=mcp_mode)
            )
        secret_values = tuple(
            filtered_env[name]
            for name in proxy_environment_allowlist
            if name in filtered_env
        ) + tuple(
            filtered_env[name]
            for name in SENSITIVE_AGENT_ENVIRONMENT_KEYS[agent]
            if name in filtered_env
        )
        redactor = Redactor(workspace=workspace, secret_values=secret_values)

        def execute(
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            stdin: str = "",
            environment: Mapping[str, str] | None = None,
            evidence_argv: Sequence[str] | None = None,
        ) -> CliCommandResult:
            """执行并记录安全证据；参数为命令上下文；返回结果；启动失败稳定化。"""
            if isinstance(argv, (str, bytes)) or not argv:
                raise ValueError("健康检查 argv 必须是非空字符串序列")
            command_argv = tuple(argv)
            if any(
                not isinstance(token, str) or "\0" in token for token in command_argv
            ):
                raise ValueError("健康检查 argv 包含非法 token")
            safe_argv = tuple(evidence_argv or command_argv)
            if any(not isinstance(token, str) or "\0" in token for token in safe_argv):
                raise ValueError("健康检查证据 argv 包含非法 token")
            command_environment = environment or filtered_env
            evidence.append(
                (
                    safe_argv,
                    "",
                    tuple(sorted(command_environment)),
                )
            )
            try:
                return self.executor.run(
                    command_argv,
                    cwd=cwd or workspace,
                    stdin=stdin,
                    timeout_seconds=self.probe_timeout_seconds,
                    env=command_environment,
                )
            except OSError:
                return CliCommandResult(
                    return_code=126,
                    stdout="",
                    stderr="command launch failed",
                )

        executable = self.which(profile.command)
        if executable is None:
            checks.append(
                CliHealthCheck(
                    key="executable",
                    label="CLI 可执行文件",
                    level="error",
                    detail="未找到可执行命令",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return self._report(agent, None, checks, filtered_env, evidence)
        try:
            executable = str(Path(executable).expanduser().resolve())
        except (OSError, RuntimeError):
            checks.append(
                CliHealthCheck(
                    key="executable",
                    label="CLI 可执行文件",
                    level="error",
                    detail="无法解析可执行命令",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return self._report(agent, None, checks, filtered_env, evidence)
        checks.append(
            CliHealthCheck(
                key="executable",
                label="CLI 可执行文件",
                level="success",
                detail=executable,
            )
        )

        version_result = execute((executable, "--version"))
        version = self._version(f"{version_result.stdout} {version_result.stderr}")
        if version_result.return_code != 0 or version is None:
            checks.append(
                CliHealthCheck(
                    key="version",
                    label="CLI 版本",
                    level="error",
                    detail="无法读取可识别版本",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return self._report(agent, None, checks, filtered_env, evidence)
        checks.append(
            CliHealthCheck(
                key="version",
                label="CLI 版本",
                level="success",
                detail=version,
            )
        )

        if isinstance(strategy, ManagedCliHealthStrategy):
            managed = strategy.run_managed_checks(
                context=CliHealthStrategyContext(
                    execute=execute,
                    workspace=workspace,
                    probe_root=self.probe_root,
                    environment=filtered_env,
                    initialize_git_repository=self._initialize_git_repository,
                ),
                executable=executable,
                version=version,
                profile=profile,
                mcp_mode=mcp_mode,
                circuit_breaker=circuit_breaker,
            )
            checks.extend(managed.checks)
            return self._report(
                agent,
                managed.identity,
                checks,
                filtered_env,
                evidence,
                mcp_configured_count=managed.mcp_configured_count,
                mcp_enabled_count=managed.mcp_enabled_count,
                mcp_list_total_count=managed.mcp_list_total_count,
                mcp_list_enabled_count=managed.mcp_list_enabled_count,
                mcp_list_healthy_count=managed.mcp_list_healthy_count,
                mcp_list_status=managed.mcp_list_status,
                mcp_mode=managed.mcp_mode,
                mcp_names=managed.mcp_names,
                mcp_names_hash=managed.mcp_names_hash,
                runtime_config_hash=(
                    managed.identity.runtime_config_hash
                    if managed.identity is not None
                    else None
                ),
                runtime_config_content_hash=managed.runtime_config_content_hash,
                runtime_config_resolved_hash=managed.runtime_config_resolved_hash,
                runtime_config_content=managed.runtime_config_content,
            )

        model = profile.model.strip() if profile.model else ""
        definition = agent_definition(agent)
        supports_variant = definition.supports_variant
        runtime_config_hash = (
            profile.runtime_config_hash if definition.freezes_runtime_config else None
        )
        if agent is AgentName.OPENCLAW and model:
            safe_configuration = json.dumps(
                {
                    "native_profile": profile.native_profile or "main",
                    "model": model,
                    "thinking": profile.variant or "off",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            runtime_config_hash = hashlib.sha256(
                safe_configuration.encode("utf-8")
            ).hexdigest()
        identity = (
            CliIdentity(
                agent=agent,
                executable=executable,
                version=version,
                model=model,
                variant=profile.variant if supports_variant else None,
                runtime_config_hash=runtime_config_hash,
                runtime_config_content_hash=(
                    profile.runtime_config_content_hash
                    if definition.freezes_runtime_config
                    else None
                ),
                runtime_config_resolved_hash=(
                    profile.runtime_config_resolved_hash
                    if definition.freezes_runtime_config
                    else None
                ),
            )
            if model
            else None
        )
        if not model:
            checks.append(
                CliHealthCheck(
                    key="model",
                    label="显式模型",
                    level="error",
                    detail="未配置可冻结的显式模型",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return self._report(agent, identity, checks, filtered_env, evidence)
        checks.append(
            CliHealthCheck(
                key="model",
                label="显式模型",
                level="success",
                detail=model,
            )
        )

        help_argv = strategy.help_argv(executable)
        help_result = execute(help_argv)
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        missing = [feature for feature in required_features if feature not in help_text]
        if help_result.return_code != 0 or missing:
            checks.append(
                CliHealthCheck(
                    key="features",
                    label="结构化流能力",
                    level="error",
                    detail="当前版本缺少所需参数",
                    category=FailureCategory.CACHE_INCOMPATIBLE,
                )
            )
            return self._report(agent, identity, checks, filtered_env, evidence)
        checks.append(
            CliHealthCheck(
                key="features",
                label="结构化流能力",
                level="success",
                detail="必需参数齐全",
            )
        )

        active_circuit = (
            circuit_breaker.active(identity)
            if circuit_breaker is not None and identity is not None
            else None
        )
        if active_circuit is not None:
            checks.append(
                CliHealthCheck(
                    key="circuit_breaker",
                    label="基础设施熔断",
                    level="error",
                    detail=("相同 CLI/版本/模型的基础设施故障仍在冷却期"),
                    category=active_circuit.category,
                )
            )
            return self._report(agent, identity, checks, filtered_env, evidence)

        auth_argv = strategy.authentication_argv(executable)
        auth = execute(auth_argv)
        if not strategy.authentication_valid(auth):
            checks.append(
                CliHealthCheck(
                    key="authentication",
                    label="登录状态",
                    level="error",
                    detail="CLI 尚未登录或凭据不可用",
                    category=FailureCategory.AUTHENTICATION_ERROR,
                )
            )
            return self._report(agent, identity, checks, filtered_env, evidence)
        checks.append(
            CliHealthCheck(
                key="authentication",
                label="登录状态",
                level="success",
                detail="凭据可用",
            )
        )

        if mcp_mode == "full":
            mcp = execute(strategy.mcp_argv(executable))
            mcp_valid = strategy.mcp_valid(mcp)
            mcp_count = len([line for line in mcp.stdout.splitlines() if line.strip()])
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="success" if mcp_valid else "warning",
                    detail=(
                        f"已读取本机 MCP，启用项 {mcp_count}"
                        if mcp_valid
                        else "可选 MCP 不可用，将不阻断模型探针"
                    ),
                    category=(
                        None if mcp_valid else FailureCategory.MCP_OPTIONAL_ERROR
                    ),
                )
            )
        else:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="success",
                    detail="精简模式：不加载本机可选 MCP",
                )
            )

        probe = execute(
            strategy.probe_argv(
                executable=executable,
                model=model,
                probe_root=self.probe_root,
            ),
            stdin='只返回 {"ok": true}，不要读取或修改工作空间。',
        )
        probe_valid = strategy.probe_has_terminal_event(probe.stdout)
        if probe.return_code != 0 or not probe_valid:
            combined = redactor.redact(f"{probe.stderr}\n{probe.stdout}", max_chars=500)
            category = (
                FailureClassifier().classify(combined)
                if probe.return_code != 0
                else FailureCategory.PROTOCOL_ERROR
            )
            checks.append(
                CliHealthCheck(
                    key="probe",
                    label="模型结构化探针",
                    level="error",
                    detail=f"探针失败：{category.value}",
                    category=category,
                )
            )
        else:
            checks.append(
                CliHealthCheck(
                    key="probe",
                    label="模型结构化探针",
                    level="success",
                    detail="模型主连接与结构化输出可用",
                )
            )
        return self._report(agent, identity, checks, filtered_env, evidence)

    @staticmethod
    def _report(
        agent: AgentName,
        identity: CliIdentity | None,
        checks: list[CliHealthCheck],
        environment: Mapping[str, str],
        evidence: list[tuple[tuple[str, ...], str, tuple[str, ...]]],
        *,
        mcp_configured_count: int | None = None,
        mcp_enabled_count: int | None = None,
        mcp_list_total_count: int | None = None,
        mcp_list_enabled_count: int | None = None,
        mcp_list_healthy_count: int | None = None,
        mcp_list_status: str | None = None,
        mcp_mode: str | None = None,
        mcp_names: tuple[str, ...] | None = None,
        mcp_names_hash: str | None = None,
        runtime_config_hash: str | None = None,
        runtime_config_content_hash: str | None = None,
        runtime_config_resolved_hash: str | None = None,
        runtime_config_content: str | None = None,
    ) -> CliHealthReport:
        """汇总报告。

        参数为 Agent/身份/检查/环境/证据；返回报告；环境只保存名称，不保存值。
        """
        report = CliHealthReport(
            agent=agent,
            checked_at=datetime.now(timezone.utc),
            identity=identity,
            checks=tuple(checks),
            can_start=not any(check.level == "error" for check in checks),
            enabled_environment_names=tuple(sorted(environment)),
            command_evidence=tuple(evidence),
            mcp_configured_count=mcp_configured_count,
            mcp_enabled_count=mcp_enabled_count,
            mcp_names_hash=mcp_names_hash,
            mcp_list_total_count=mcp_list_total_count,
            mcp_list_enabled_count=mcp_list_enabled_count,
            mcp_list_healthy_count=mcp_list_healthy_count,
            mcp_list_status=mcp_list_status,
            runtime_config_hash=runtime_config_hash,
            runtime_config_content_hash=runtime_config_content_hash,
            runtime_config_resolved_hash=runtime_config_resolved_hash,
        )
        if runtime_config_content is None:
            return report
        if (
            mcp_mode not in {"minimal", "full"}
            or mcp_names is None
            or mcp_configured_count is None
            or mcp_names_hash is None
            or runtime_config_hash is None
            or runtime_config_content_hash is None
            or runtime_config_resolved_hash is None
        ):
            raise ValueError("健康报告缺少运行配置能力元数据")
        capability = _seal_runtime_config_capability(
            content=runtime_config_content,
            mcp_mode=mcp_mode,
            mcp_names=mcp_names,
            mcp_configured_count=mcp_configured_count,
            mcp_names_hash=mcp_names_hash,
            runtime_config_hash=runtime_config_hash,
            runtime_config_content_hash=runtime_config_content_hash,
            runtime_config_resolved_hash=runtime_config_resolved_hash,
        )
        return report.with_internal_runtime_config(capability)
