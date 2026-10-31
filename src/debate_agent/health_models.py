"""CLI 健康检查的公共模型、协议、错误分类和基础策略。"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, PrivateAttr

from debate_agent.config import (
    AgentProfile,
    _RuntimeConfigCapability,
)
from debate_agent.models import AgentName
from debate_agent.process import ProcessTermination

if TYPE_CHECKING:
    from debate_agent.circuit_breaker import CircuitBreakerStore

CLI_DEFAULT_MODEL = "__cli_default__"
OPENCODE_RESOLVED_CONFIG_MAX_BYTES = 128 * 1024
OPENCODE_MCP_MAX_COUNT = 256
OPENCODE_MCP_LIST_MAX_BYTES = 128 * 1024
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
OPENCODE_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
OPENCODE_MCP_LIST_ENTRY_PATTERN = re.compile(
    r"^\s*(?:│\s*)?ℹ\s+(?P<icon>[○✓⚠✗])\s+(?P<name>.+?)\s+"
    r"(?P<status>needs client registration|needs authentication|"
    r"not initialized|connected|disabled|failed)\s*$"
)
OPENCODE_MCP_LIST_TOTAL_PATTERN = re.compile(
    r"^\s*(?:└\s*)?(?P<count>\d+) server\(s\)\s*$"
)


class FailureCategory(str, Enum):
    """定义 Supervisor 可机械处理的调用失败类别。"""

    STARTUP_TIMEOUT = "startup_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    HARD_TIMEOUT = "hard_timeout"
    NETWORK_ERROR = "network_error"
    CACHE_INCOMPATIBLE = "cache_incompatible"
    MCP_OPTIONAL_ERROR = "mcp_optional_error"
    PROTOCOL_ERROR = "protocol_error"
    VALIDATION_ERROR = "validation_error"
    POLICY_VIOLATION = "policy_violation"
    WORKSPACE_RACE = "workspace_race"
    AUTHENTICATION_ERROR = "authentication_error"
    AMBIGUOUS_TRANSPORT = "ambiguous_transport"
    CONFIGURATION = "configuration_error"
    PROCESS_ERROR = "process_error"


class FailureClassifier:
    """把稳定错误信号归入 Supervisor 自动动作类别。"""

    def classify(
        self,
        message: str,
        *,
        termination: ProcessTermination | None = None,
    ) -> FailureCategory:
        """分类调用失败。

        参数为脱敏错误和可选进程终止类型；返回类别；未知错误归为进程错误。
        """
        termination_categories = {
            ProcessTermination.STARTUP_TIMEOUT: FailureCategory.STARTUP_TIMEOUT,
            ProcessTermination.IDLE_TIMEOUT: FailureCategory.IDLE_TIMEOUT,
            ProcessTermination.HARD_TIMEOUT: FailureCategory.HARD_TIMEOUT,
        }
        if termination in termination_categories:
            return termination_categories[termination]
        normalized = message.casefold()
        if any(
            marker in normalized
            for marker in (
                "未经批准",
                "未批准命令",
                "policy violation",
                "unauthorized command",
                "越界写入",
            )
        ):
            return FailureCategory.POLICY_VIOLATION
        if any(
            marker in normalized
            for marker in (
                "supports_reasoning_summaries",
                "cache incompatible",
                "model cache incompatible",
                "缓存字段",
            )
        ):
            return FailureCategory.CACHE_INCOMPATIBLE
        if any(
            marker in normalized
            for marker in (
                "tls",
                "websocket",
                "dns",
                "connection reset",
                "connection refused",
                "network",
                "timed out connecting",
            )
        ):
            return FailureCategory.NETWORK_ERROR
        if "mcp" in normalized:
            return FailureCategory.MCP_OPTIONAL_ERROR
        if any(marker in normalized for marker in ("schema", "json", "protocol")):
            return FailureCategory.PROTOCOL_ERROR
        if "validation" in normalized or "pytest" in normalized:
            return FailureCategory.VALIDATION_ERROR
        if "workspace" in normalized and "race" in normalized:
            return FailureCategory.WORKSPACE_RACE
        if any(
            marker in normalized
            for marker in (
                "http 401",
                "unauthorized",
                "authentication",
                "not logged in",
                "invalid api key",
                "api key is required",
            )
        ):
            return FailureCategory.AUTHENTICATION_ERROR
        return FailureCategory.PROCESS_ERROR


class CliCommandResult(BaseModel):
    """保存健康探针的一次有限命令结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    return_code: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    """声明 CLI 健康探针所需的可注入命令执行接口。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """执行探针；参数为 argv/上下文，返回有限结果，启动错误可抛异常。"""
        ...


class SubprocessCommandExecutor:
    """使用无 shell 的短进程执行健康探针。"""

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """终止并回收健康探针进程树。

        参数为已启动进程；无返回值；进程已经退出时忽略对应系统错误。
        """
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.communicate()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """执行短探针。

        参数为 argv、目录、stdin、时限和过滤环境；返回结果；启动失败传播 OSError。
        """
        process = subprocess.Popen(
            tuple(argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(env),
            shell=False,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(
                input=stdin,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            return CliCommandResult(
                return_code=124,
                stdout="",
                stderr="health probe timeout",
            )
        return CliCommandResult(
            return_code=process.returncode,
            stdout=stdout[-200_000:],
            stderr=stderr[-20_000:],
        )


class CliIdentity(BaseModel):
    """保存已验证并可冻结到 Run 的 CLI 身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentName
    executable: str
    version: str
    model: str
    variant: str | None = None
    runtime_config_hash: str | None = None
    runtime_config_content_hash: str | None = None
    runtime_config_resolved_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _OpenCodeMcpListSummary:
    """保存 MCP list 的脱敏聚合信息，不保存名称或原始输出。"""

    total_count: int
    enabled_count: int
    healthy_count: int
    status: str
    names_hash: str


class CliHealthCheck(BaseModel):
    """保存一项不含凭据的健康检查结论。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    level: str
    detail: str
    category: FailureCategory | None = None


class CliHealthReport(BaseModel):
    """汇总一个 CLI 的预检结论与安全命令证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentName
    checked_at: datetime
    identity: CliIdentity | None
    checks: tuple[CliHealthCheck, ...]
    can_start: bool
    enabled_environment_names: tuple[str, ...]
    mcp_configured_count: int | None = None
    mcp_enabled_count: int | None = None
    mcp_names_hash: str | None = None
    mcp_list_total_count: int | None = None
    mcp_list_enabled_count: int | None = None
    mcp_list_healthy_count: int | None = None
    mcp_list_status: str | None = None
    runtime_config_hash: str | None = None
    runtime_config_content_hash: str | None = None
    runtime_config_resolved_hash: str | None = None
    command_evidence: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = ()

    def by_key(self, key: str) -> CliHealthCheck:
        """按键查找检查；参数为键，返回检查，缺失时抛 KeyError。"""
        return next(check for check in self.checks if check.key == key)

    _runtime_config_capability: _RuntimeConfigCapability | None = PrivateAttr(
        default=None
    )

    def with_internal_runtime_config(
        self, capability: _RuntimeConfigCapability
    ) -> CliHealthReport:
        """附加内部运行配置；参数为封印能力；返回私有副本；摘要不匹配时抛错。"""
        if type(capability) is not _RuntimeConfigCapability or not capability.verify():
            raise ValueError("健康报告运行配置能力封印无效")
        if capability.hashes() != (
            self.runtime_config_hash,
            self.runtime_config_content_hash,
            self.runtime_config_resolved_hash,
        ):
            raise ValueError("健康报告运行配置摘要不匹配")
        frozen = self.model_copy()
        frozen._runtime_config_capability = capability
        return frozen

    def internal_runtime_config_capability(
        self,
    ) -> _RuntimeConfigCapability | None:
        """读取健康报告能力；无参数；返回已验证能力或 None；篡改时抛错。"""
        private = self.__pydantic_private__ or {}
        if (
            "_runtime_config_content" in self.__dict__
            or "_runtime_config_content" in private
            or "_runtime_config_capability" in self.__dict__
        ):
            raise ValueError("检测到未授权健康报告运行配置注入")
        capability = private.get("_runtime_config_capability")
        if capability is None:
            return None
        if type(capability) is not _RuntimeConfigCapability or not capability.verify():
            raise ValueError("健康报告运行配置能力封印校验失败")
        if capability.hashes() != (
            self.runtime_config_hash,
            self.runtime_config_content_hash,
            self.runtime_config_resolved_hash,
        ):
            raise ValueError("健康报告运行配置能力与摘要不匹配")
        return capability

    def internal_runtime_config_content(self) -> str | None:
        """读取内部运行配置；无参数；返回当前进程私有内容或 None；篡改时抛错。"""
        capability = self.internal_runtime_config_capability()
        return capability.content() if capability is not None else None


class CliPreflightError(RuntimeError):
    """表示新 Run 的关键 CLI 健康检查未通过。"""


class CliHealthRegistryError(LookupError):
    """表示 Agent 没有注册受信任的 CLI 健康策略。"""


def filter_cli_environment(
    source: Mapping[str, str],
    *,
    base_names: Sequence[str],
    proxy_names: Sequence[str],
) -> dict[str, str]:
    """过滤 CLI 子进程环境。

    参数为源环境、基础名和显式代理/证书名；返回含值的运行时字典；不修改源环境。
    """
    allowed = set(base_names) | set(proxy_names)
    return {name: value for name, value in source.items() if name in allowed}


class CliHealthStrategy(Protocol):
    """声明内置 CLI 厂商差异所需的健康检查策略边界。"""

    def discover_model(
        self, *, home: Path, environment: Mapping[str, str]
    ) -> str | None:
        """发现模型；参数为 HOME/环境，返回显式模型或 None，损坏配置被忽略。"""
        ...

    def help_argv(self, executable: str) -> tuple[str, ...]:
        """构建 help 命令；参数为可执行文件，返回 argv，不抛异常。"""
        ...

    def required_features(self) -> tuple[str, ...]:
        """返回必需特征；无参数，返回参数标记元组，不抛异常。"""
        ...

    def authentication_argv(self, executable: str) -> tuple[str, ...]:
        """构建认证命令；参数为可执行文件，返回 argv，不抛异常。"""
        ...

    def authentication_valid(self, result: CliCommandResult) -> bool:
        """验证认证结果；参数为命令结果，返回是否有效，不抛异常。"""
        ...

    def mcp_argv(self, executable: str) -> tuple[str, ...]:
        """构建 MCP 命令；参数为可执行文件，返回 argv，不抛异常。"""
        ...

    def mcp_valid(self, result: CliCommandResult) -> bool:
        """验证 MCP 结果；参数为命令结果，返回是否有效，不抛异常。"""
        ...

    def probe_argv(
        self, *, executable: str, model: str, probe_root: Path
    ) -> tuple[str, ...]:
        """构建模型探针；参数为命令/模型/目录，返回 argv，写入失败时抛 OSError。"""
        ...

    def probe_has_terminal_event(self, stdout: str) -> bool:
        """验证终止事件；参数为 stdout，返回是否命中厂商协议，不抛异常。"""
        ...


class HealthCommandRunner(Protocol):
    """声明 Strategy 通过 Checker 执行受控健康命令的强类型回调。"""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: str = "",
        environment: Mapping[str, str] | None = None,
        evidence_argv: Sequence[str] | None = None,
    ) -> CliCommandResult:
        """执行命令；参数为 argv/上下文/脱敏证据；返回结果；执行错误可传播。"""
        ...


class GitProbeInitializer(Protocol):
    """声明 Checker 为 Strategy 初始化临时 Git 探针库的回调。"""

    def __call__(self, path: Path) -> bool:
        """初始化仓库；参数为临时目录；返回是否成功；不回显输出。"""
        ...


class CliHealthStrategyContext:
    """保存受管 Strategy 可用的 Checker 执行边界。"""

    __slots__ = (
        "environment",
        "execute",
        "initialize_git_repository",
        "probe_root",
        "workspace",
    )

    def __init__(
        self,
        *,
        execute: HealthCommandRunner,
        workspace: Path,
        probe_root: Path,
        environment: Mapping[str, str],
        initialize_git_repository: GitProbeInitializer,
    ) -> None:
        """创建策略上下文；参数为受控执行依赖；无返回值；不复制敏感值到表示。"""
        self.execute = execute
        self.workspace = workspace
        self.probe_root = probe_root
        self.environment = environment
        self.initialize_git_repository = initialize_git_repository

    def __repr__(self) -> str:
        """生成安全表示；无参数；返回固定文本；不包含环境与路径。"""
        return "<CliHealthStrategyContext>"


class CliHealthStrategyResult:
    """保存受管 Strategy 返回给 Checker 的安全检查结果。"""

    __slots__ = (
        "checks",
        "identity",
        "mcp_configured_count",
        "mcp_enabled_count",
        "mcp_list_enabled_count",
        "mcp_list_healthy_count",
        "mcp_list_status",
        "mcp_list_total_count",
        "mcp_mode",
        "mcp_names",
        "mcp_names_hash",
        "runtime_config_content",
        "runtime_config_content_hash",
        "runtime_config_resolved_hash",
    )

    def __init__(
        self,
        *,
        identity: CliIdentity | None,
        checks: tuple[CliHealthCheck, ...],
        runtime_config_content: str | None = None,
        runtime_config_content_hash: str | None = None,
        mcp_configured_count: int | None = None,
        mcp_enabled_count: int | None = None,
        mcp_list_total_count: int | None = None,
        mcp_list_enabled_count: int | None = None,
        mcp_list_healthy_count: int | None = None,
        mcp_list_status: str | None = None,
        mcp_mode: str | None = None,
        mcp_names: tuple[str, ...] | None = None,
        mcp_names_hash: str | None = None,
        runtime_config_resolved_hash: str | None = None,
    ) -> None:
        """创建策略结果；参数为安全结论与内部配置；无返回值；不公开配置表示。"""
        self.identity = identity
        self.checks = checks
        self.runtime_config_content = runtime_config_content
        self.runtime_config_content_hash = runtime_config_content_hash
        self.mcp_configured_count = mcp_configured_count
        self.mcp_enabled_count = mcp_enabled_count
        self.mcp_list_total_count = mcp_list_total_count
        self.mcp_list_enabled_count = mcp_list_enabled_count
        self.mcp_list_healthy_count = mcp_list_healthy_count
        self.mcp_list_status = mcp_list_status
        self.mcp_mode = mcp_mode
        self.mcp_names = mcp_names
        self.mcp_names_hash = mcp_names_hash
        self.runtime_config_resolved_hash = runtime_config_resolved_hash

    def __repr__(self) -> str:
        """生成安全表示；无参数；返回固定文本；不包含配置内容。"""
        return "<CliHealthStrategyResult>"


@runtime_checkable
class ManagedCliHealthStrategy(Protocol):
    """声明需要自定义阶段顺序但仍由 Checker 执行命令的 Strategy hook。"""

    def initial_environment(
        self, *, base: Mapping[str, str], mcp_mode: str
    ) -> Mapping[str, str]:
        """准备初始环境；参数为过滤环境/MCP 模式；返回新映射；非法模式可抛错。"""
        ...

    def run_managed_checks(
        self,
        *,
        context: CliHealthStrategyContext,
        executable: str,
        version: str,
        profile: AgentProfile,
        mcp_mode: str,
        circuit_breaker: CircuitBreakerStore | None,
    ) -> CliHealthStrategyResult:
        """运行厂商阶段；参数为上下文/身份/Profile；返回安全结果；执行错误可传播。"""
        ...


class _BaseCliHealthStrategy:
    """提供内置 CLI 健康策略的通用结果判断。"""

    @staticmethod
    def authentication_valid(result: CliCommandResult) -> bool:
        """验证认证结果；参数为命令结果，返回退出码是否为零，不抛异常。"""
        return result.return_code == 0

    @staticmethod
    def mcp_argv(executable: str) -> tuple[str, ...]:
        """构建 MCP 命令；参数为可执行文件，返回统一 list argv，不抛异常。"""
        return executable, "mcp", "list"

    @staticmethod
    def mcp_valid(result: CliCommandResult) -> bool:
        """验证 MCP 结果；参数为命令结果，返回退出码是否为零，不抛异常。"""
        return result.return_code == 0

    @staticmethod
    def _has_terminal(stdout: str, event_type: str) -> bool:
        """查找终止事件；参数为 stdout/事件类型，返回是否命中，无效 JSON 被忽略。"""
        for line in stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("type") == event_type:
                return True
        return False
