"""CLI Adapter 的公共协议、调用模型、安全环境和基础解析器。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from debate_agent.config import AgentProfile
from debate_agent.invocation_events import InvocationEventType, InvocationSeverity
from debate_agent.models import AgentName
from debate_agent.policy import CommandPolicy
from debate_agent.process import (
    ProcessOutput,
    ProcessResult,
    PromptTransport,
    TemporaryInput,
)


class AdapterOutputError(RuntimeError):
    """表示 CLI 成功退出但没有返回合法结构化协议。"""


class SessionBoundaryError(RuntimeError):
    """表示原生 Session 续接无法证明仍满足写入边界。"""


class AdapterRegistryError(LookupError):
    """表示 Agent 没有注册受信任的 Adapter factory。"""


@dataclass(frozen=True, slots=True)
class Invocation:
    """保存不含 shell 拼接的 CLI 调用参数。"""

    argv: tuple[str, ...] = field(repr=False)
    cwd: Path
    stdin: str | None
    timeout_seconds: int
    startup_timeout_seconds: int
    idle_timeout_seconds: int
    hard_timeout_seconds: int
    heartbeat_interval_seconds: int
    session_id: str | None
    audit_argv: tuple[str, ...] = ()
    prompt_transport: PromptTransport = PromptTransport.STDIN
    temporary_inputs: tuple[TemporaryInput, ...] = field(
        default=(),
        repr=False,
    )
    stdout_persist_transform: Callable[[str], str] | None = field(
        default=None,
        repr=False,
    )
    environment_overrides: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        """补齐安全审计 argv。

        参数：使用当前 Invocation 的 argv 与可选 audit_argv。
        返回值：无。
        异常：不抛出异常；未提供审计参数时使用不含 Prompt 的原 argv。
        """
        if not self.audit_argv:
            object.__setattr__(self, "audit_argv", self.argv)


@dataclass(frozen=True, slots=True)
class VendorTerminal:
    """保存 Vendor Adapter 从终止事件中提取的 Session 和原始内容。"""

    session_id: str | None
    content: object


@dataclass(frozen=True, slots=True)
class AdapterStreamEvent:
    """保存 Adapter 从厂商输出中提取的受限实时事件草稿。"""

    event_type: InvocationEventType
    title: str
    summary: str = ""
    severity: InvocationSeverity = InvocationSeverity.INFO
    changed_paths: tuple[str, ...] = ()
    useful: bool = False
    terminal: bool = False


class AgentStreamParser(Protocol):
    """声明 Runtime 消费厂商增量输出所需的解析边界。"""

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费增量输出；参数为进程输出，返回安全事件，解析错误可抛异常。"""
        ...


class AgentAdapter(Protocol):
    """声明 Runtime 调用任一内置 Agent 所需的统一 Adapter 边界。"""

    agent: AgentName

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> AgentStreamParser:
        """创建流解析器；参数为可选工作区，返回独立解析器，创建错误可抛异常。"""
        ...

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 CLI 调用；参数为工作树/Prompt/Session，返回调用，边界错误时抛错。"""
        ...

    def extract_terminal(self, result: ProcessResult) -> VendorTerminal:
        """提取终止内容；参数为进程结果，返回厂商终止对象，协议无效时抛错。"""
        ...

    def failure_detail(self, stdout: str) -> str | None:
        """提取失败详情；参数为 stdout，返回安全摘要或 None，不抛异常。"""
        ...


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """保存 Adapter factory 所需的冻结运行上下文。"""

    schema_path: Path
    mcp_mode: Literal["minimal", "full"]
    unrestricted_commands: bool
    codex_resume_sandbox_verified: bool
    command_policy: CommandPolicy


AdapterFactory = Callable[[AgentProfile, AdapterContext], AgentAdapter]


ENVIRONMENT_OVERRIDE_ALLOWLIST: dict[AgentName, frozenset[str]] = {
    AgentName.CODEX: frozenset(),
    AgentName.CLAUDE: frozenset(),
    AgentName.QODER: frozenset(),
    AgentName.OPENCODE: frozenset(
        {
            "OPENCODE_CONFIG_CONTENT",
            "OPENCODE_DISABLE_AUTOUPDATE",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        }
    ),
    AgentName.OPENCLAW: frozenset(),
    AgentName.HERMES: frozenset(),
    AgentName.CODEBUDDY: frozenset(
        {
            "CODEBUDDY_DISABLE_AUTO_MEMORY",
            "CODEBUDDY_DISABLE_HOT_RELOAD",
            "CODEBUDDY_PROMPT_SUGGESTION_DISABLED",
            "CODEBUDDY_SKIP_BUILTIN_MARKETPLACE",
            "DISABLE_AUTOUPDATER",
            "DISABLE_ERROR_REPORTING",
            "DISABLE_TELEMETRY",
        }
    ),
}

SENSITIVE_ENVIRONMENT_OVERRIDE_KEYS: dict[AgentName, frozenset[str]] = {
    AgentName.CODEX: frozenset(),
    AgentName.CLAUDE: frozenset(),
    AgentName.QODER: frozenset(),
    AgentName.OPENCODE: frozenset({"OPENCODE_CONFIG_CONTENT"}),
    AgentName.OPENCLAW: frozenset(),
    AgentName.HERMES: frozenset(),
    AgentName.CODEBUDDY: frozenset(),
}

AGENT_ENVIRONMENT_PASSTHROUGH: dict[AgentName, frozenset[str]] = {
    AgentName.CODEX: frozenset(),
    AgentName.CLAUDE: frozenset(),
    AgentName.QODER: frozenset(),
    AgentName.OPENCODE: frozenset(),
    AgentName.OPENCLAW: frozenset(),
    AgentName.HERMES: frozenset(),
    AgentName.CODEBUDDY: frozenset(
        {
            "CODEBUDDY_API_KEY",
            "CODEBUDDY_AUTH_TOKEN",
            "CODEBUDDY_BASE_URL",
            "CODEBUDDY_CUSTOM_HEADERS",
            "CODEBUDDY_INTERNET_ENVIRONMENT",
        }
    ),
}

SENSITIVE_AGENT_ENVIRONMENT_KEYS: dict[AgentName, frozenset[str]] = {
    agent: (
        frozenset(
            {
                "CODEBUDDY_API_KEY",
                "CODEBUDDY_AUTH_TOKEN",
                "CODEBUDDY_CUSTOM_HEADERS",
            }
        )
        if agent is AgentName.CODEBUDDY
        else frozenset()
    )
    for agent in AgentName
}


def _codex_compatible_schema_path(schema_path: Path) -> Path:
    """生成 Codex 可接受的响应 Schema。

    参数为完整 Draft 2020-12 Schema 路径；返回用于 Codex CLI 的缓存副本。
    Codex 的 response_format 目前拒绝 ``allOf``、``if``、``then`` 与 ``else``。
    这些跨字段规则仍由本地 Draft 校验和 Pydantic 协议模型强制执行，因此只在
    CLI 传输层移除，不能修改原始 Schema。
    """

    def strip_unsupported(value: object) -> object:
        """移除 Codex 不支持的条件关键字；参数为 Schema 节点，返回副本。"""
        if isinstance(value, dict):
            copied = {
                key: strip_unsupported(item)
                for key, item in value.items()
                if key not in {"allOf", "if", "then", "else"}
            }
            if "type" not in copied:
                literal = copied.get("const")
                values = copied.get("enum", [literal] if "const" in copied else [])
                if isinstance(values, list) and values:
                    inferred = {
                        "boolean"
                        if isinstance(item, bool)
                        else "integer"
                        if isinstance(item, int)
                        else "number"
                        if isinstance(item, float)
                        else "string"
                        if isinstance(item, str)
                        else "null"
                        if item is None
                        else "object"
                        if isinstance(item, dict)
                        else "array"
                        if isinstance(item, list)
                        else "unknown"
                        for item in values
                    }
                    if "unknown" not in inferred:
                        copied["type"] = (
                            inferred.pop() if len(inferred) == 1 else sorted(inferred)
                        )
            return copied
        if isinstance(value, list):
            return [strip_unsupported(item) for item in value]
        return value

    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    compatible = strip_unsupported(payload)
    if compatible == payload:
        return schema_path
    serialized = json.dumps(
        compatible,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    cache_dir = Path(tempfile.gettempdir()) / "debate-agent-codex-schemas"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{schema_path.stem}-{digest}.json"
    if not target.is_file():
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=cache_dir,
                prefix=f".{schema_path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(serialized)
                temporary.flush()
                temporary_path = Path(temporary.name)
            temporary_path.replace(target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    return target


def _safe_changed_paths(
    values: Iterable[object], *, workspace: Path | None
) -> tuple[str, ...]:
    """过滤厂商变更路径。

    参数为候选值和可选工作区；返回安全相对路径；非法路径被丢弃而不抛异常。
    """
    safe: list[str] = []
    root = workspace.expanduser().resolve() if workspace is not None else None
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            candidate = Path(value).expanduser()
        except RuntimeError:
            continue
        if candidate.is_absolute():
            if root is None:
                continue
            try:
                text = candidate.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
        else:
            pure = PurePosixPath(value.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                continue
            text = pure.as_posix()
        text = text[:512]
        if text not in {"", "."} and text not in safe:
            safe.append(text)
    return tuple(safe)


class _BaseStreamParser:
    """提供厂商实时解析器共用的错误去重和安全事件构造。"""

    def __init__(self, *, workspace: Path | None = None) -> None:
        """创建解析器；参数为可选工作区，返回实例，不执行文件读取。"""
        self.workspace = workspace
        self._seen_errors: set[str] = set()

    @staticmethod
    def _event(
        event_type: InvocationEventType,
        title: str,
        *,
        summary: object = "",
        severity: InvocationSeverity = InvocationSeverity.INFO,
        changed_paths: tuple[str, ...] = (),
        useful: bool = False,
        terminal: bool = False,
    ) -> AdapterStreamEvent:
        """构造受限事件。

        参数为允许的展示字段；返回裁剪事件；该方法不传播厂商未知字段。
        """
        return AdapterStreamEvent(
            event_type=event_type,
            title=str(title)[:160],
            summary=str(summary or "")[:500],
            severity=severity,
            changed_paths=changed_paths,
            useful=useful,
            terminal=terminal,
        )

    def _error_event(self, message: object) -> tuple[AdapterStreamEvent, ...]:
        """生成去重错误事件。

        参数为厂商错误摘要；返回零或一条 warning；重复错误不会刷新活动。
        """
        normalized = str(message or "CLI 报告未知错误").replace("\n", " ")[:500]
        fingerprint = normalized.casefold()
        if fingerprint in self._seen_errors:
            return ()
        self._seen_errors.add(fingerprint)
        return (
            self._event(
                InvocationEventType.ERROR,
                "CLI 报告错误",
                summary=normalized,
                severity=InvocationSeverity.WARNING,
                useful=False,
            ),
        )


class _BaseAdapter:
    """提供各 CLI 共用的进程、Session 和终止事件提取逻辑。"""

    agent: AgentName

    def __init__(self, profile: AgentProfile, *, schema_path: Path) -> None:
        """创建基础 Adapter；参数为 Profile 与 Schema，返回实例，文件缺失时抛错。"""
        resolved_schema = schema_path.expanduser().resolve()
        if not resolved_schema.is_file():
            raise ValueError(f"Schema 文件不存在: {resolved_schema}")
        self.profile = profile
        self.schema_path = resolved_schema

    def _decode(self, stdout: str) -> tuple[str | None, object]:
        """解码厂商输出；参数为 stdout，返回 Session 与原始内容，子类必须实现。"""
        raise AdapterOutputError("Adapter 缺少厂商输出解码器")

    def _decode_result(self, result: ProcessResult) -> tuple[str | None, object]:
        """解码进程结果；参数为结果，返回 Session/内容，截断时抛协议错误。"""
        if result.stdout_truncated:
            raise AdapterOutputError("CLI 结构化输出被截断")
        return self._decode(result.stdout)

    def failure_detail(self, stdout: str) -> str | None:
        """提取厂商失败详情；参数为 stdout，返回可安全展示的短文本，默认无详情。"""
        return None

    def extract_terminal(self, result: ProcessResult) -> VendorTerminal:
        """提取厂商终止事件。

        参数为进程结果；返回 Session 和未解析内容；无内存输出且无完整 raw spool，
        或厂商事件无效时抛协议错误。
        """
        has_raw_spool = (
            result.stdout_truncated
            and result.stdout_path is not None
            and result.stdout_path.is_file()
        )
        if not result.stdout.strip() and not has_raw_spool:
            raise AdapterOutputError("CLI 返回空响应")
        try:
            session_id, content = self._decode_result(result)
        except AdapterOutputError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AdapterOutputError("CLI 厂商终止输出无效") from exc
        return VendorTerminal(session_id=session_id, content=content)
