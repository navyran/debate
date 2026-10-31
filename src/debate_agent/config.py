"""运行配置及输入规范化。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from debate_agent.agent_catalog import agent_definitions
from debate_agent.models import AgentName, ReviewProtocolVersion

_RUNTIME_CONFIG_CAPABILITY_SECRET = secrets.token_bytes(32)


class _RuntimeConfigCapability:
    """保存不可序列化的进程内运行配置能力。"""

    __slots__ = (
        "__content",
        "__mcp_configured_count",
        "__mcp_mode",
        "__mcp_names",
        "__mcp_names_hash",
        "__runtime_config_content_hash",
        "__runtime_config_hash",
        "__runtime_config_resolved_hash",
        "__signature",
    )

    def __init__(
        self,
        content: str,
        *,
        mcp_mode: Literal["minimal", "full"],
        mcp_names: tuple[str, ...],
        mcp_configured_count: int,
        mcp_names_hash: str,
        runtime_config_hash: str,
        runtime_config_content_hash: str,
        runtime_config_resolved_hash: str,
        signature: str,
    ) -> None:
        """创建内部能力；参数为配置及预检元数据；无返回值；仅内部工厂调用。"""
        self.__content = content
        self.__mcp_mode = mcp_mode
        self.__mcp_names = mcp_names
        self.__mcp_configured_count = mcp_configured_count
        self.__mcp_names_hash = mcp_names_hash
        self.__runtime_config_hash = runtime_config_hash
        self.__runtime_config_content_hash = runtime_config_content_hash
        self.__runtime_config_resolved_hash = runtime_config_resolved_hash
        self.__signature = signature

    def _signed_payload(self) -> bytes:
        """生成签名载荷；无参数；返回规范字节；不包含对象表示。"""
        return _runtime_config_capability_payload(
            content=self.__content,
            mcp_mode=self.__mcp_mode,
            mcp_names=self.__mcp_names,
            mcp_configured_count=self.__mcp_configured_count,
            mcp_names_hash=self.__mcp_names_hash,
            runtime_config_hash=self.__runtime_config_hash,
            runtime_config_content_hash=self.__runtime_config_content_hash,
            runtime_config_resolved_hash=self.__runtime_config_resolved_hash,
        )

    def verify(self) -> bool:
        """验证进程内能力封印；无参数；返回是否未被修改；不抛异常。"""
        try:
            expected = hmac.digest(
                _RUNTIME_CONFIG_CAPABILITY_SECRET,
                self._signed_payload(),
                "sha256",
            ).hex()
            return hmac.compare_digest(self.__signature, expected)
        except (AttributeError, TypeError, ValueError):
            return False

    def content(self) -> str:
        """读取内部内容；无参数；返回运行配置；不抛异常。"""
        return self.__content

    def metadata(self) -> tuple[str, tuple[str, ...], int, str]:
        """读取 MCP 元数据；无参数；返回模式/名称/计数/摘要；不抛异常。"""
        return (
            self.__mcp_mode,
            self.__mcp_names,
            self.__mcp_configured_count,
            self.__mcp_names_hash,
        )

    def hashes(self) -> tuple[str, str, str]:
        """读取配置摘要；无参数；返回身份/内容/resolved 摘要；不抛异常。"""
        return (
            self.__runtime_config_hash,
            self.__runtime_config_content_hash,
            self.__runtime_config_resolved_hash,
        )

    def __repr__(self) -> str:
        """生成安全表示；无参数；返回固定文本；不包含配置内容。"""
        return "<_RuntimeConfigCapability>"


def _runtime_config_capability_payload(
    *,
    content: str,
    mcp_mode: str,
    mcp_names: tuple[str, ...],
    mcp_configured_count: int,
    mcp_names_hash: str,
    runtime_config_hash: str,
    runtime_config_content_hash: str,
    runtime_config_resolved_hash: str,
) -> bytes:
    """规范化能力载荷；参数为配置与摘要；返回签名字节；序列化失败时抛错。"""
    return json.dumps(
        {
            "content": content,
            "mcp_configured_count": mcp_configured_count,
            "mcp_mode": mcp_mode,
            "mcp_names": mcp_names,
            "mcp_names_hash": mcp_names_hash,
            "runtime_config_content_hash": runtime_config_content_hash,
            "runtime_config_hash": runtime_config_hash,
            "runtime_config_resolved_hash": runtime_config_resolved_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seal_runtime_config_capability(
    *,
    content: str,
    mcp_mode: str,
    mcp_names: tuple[str, ...],
    mcp_configured_count: int,
    mcp_names_hash: str,
    runtime_config_hash: str,
    runtime_config_content_hash: str,
    runtime_config_resolved_hash: str,
) -> _RuntimeConfigCapability:
    """封印预检运行配置；参数为内容及安全元数据；返回进程内能力；非法值抛错。"""
    if mcp_mode not in {"minimal", "full"}:
        raise ValueError("OpenCode MCP 模式非法")
    validated_mcp_mode: Literal["minimal", "full"] = (
        "minimal" if mcp_mode == "minimal" else "full"
    )
    if mcp_configured_count != len(mcp_names):
        raise ValueError("OpenCode MCP 计数不一致")
    if any(not isinstance(name, str) or not name.strip() for name in mcp_names):
        raise ValueError("OpenCode MCP 名称非法")
    expected_names_hash = hashlib.sha256(
        json.dumps(
            sorted(mcp_names), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if expected_names_hash != mcp_names_hash:
        raise ValueError("OpenCode MCP 名称摘要不一致")
    hashes = (
        runtime_config_hash,
        runtime_config_content_hash,
        runtime_config_resolved_hash,
    )
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        raise ValueError("OpenCode 运行配置摘要非法")
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != (
        runtime_config_content_hash
    ):
        raise ValueError("OpenCode 运行配置内容摘要不一致")
    payload = _runtime_config_capability_payload(
        content=content,
        mcp_mode=validated_mcp_mode,
        mcp_names=mcp_names,
        mcp_configured_count=mcp_configured_count,
        mcp_names_hash=mcp_names_hash,
        runtime_config_hash=runtime_config_hash,
        runtime_config_content_hash=runtime_config_content_hash,
        runtime_config_resolved_hash=runtime_config_resolved_hash,
    )
    signature = hmac.digest(
        _RUNTIME_CONFIG_CAPABILITY_SECRET, payload, "sha256"
    ).hex()
    return _RuntimeConfigCapability(
        content,
        mcp_mode=validated_mcp_mode,
        mcp_names=mcp_names,
        mcp_configured_count=mcp_configured_count,
        mcp_names_hash=mcp_names_hash,
        runtime_config_hash=runtime_config_hash,
        runtime_config_content_hash=runtime_config_content_hash,
        runtime_config_resolved_hash=runtime_config_resolved_hash,
        signature=signature,
    )


def infer_target_branch(workspace: str | Path) -> str:
    """推断目标分支；参数为工作目录，返回当前分支或 Git 默认初始分支。"""
    resolved = Path(workspace).expanduser().resolve()
    current = subprocess.run(
        ["git", "-C", str(resolved), "branch", "--show-current"],
        check=False,
        capture_output=True,
        text=True,
    )
    branch = current.stdout.strip()
    if current.returncode == 0 and branch:
        return branch
    configured = subprocess.run(
        ["git", "config", "--get", "init.defaultBranch"],
        check=False,
        capture_output=True,
        text=True,
    )
    return configured.stdout.strip() or "master"


class AgentProfile(BaseModel):
    """描述一个 CLI Agent 的冻结运行参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str
    model: str | None = None
    variant: str | None = None
    native_profile: str | None = None
    provider: str | None = None
    version: str | None = None
    runtime_config_hash: str | None = None
    runtime_config_content_hash: str | None = None
    runtime_config_resolved_hash: str | None = None
    timeout_seconds: int = Field(default=3600, gt=0)
    startup_timeout_seconds: int = Field(default=300, gt=0)
    idle_timeout_seconds: int = Field(default=300, gt=0)
    hard_timeout_seconds: int = Field(default=3600, gt=0)
    heartbeat_interval_seconds: int = Field(default=5, gt=0)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_timeout(cls, value: Any) -> Any:
        """迁移历史硬超时。

        参数为待校验 Profile；返回补齐硬上限的载荷；非法字段仍由 Pydantic 抛错。
        """
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "timeout_seconds" in migrated and "hard_timeout_seconds" not in migrated:
            migrated["hard_timeout_seconds"] = migrated["timeout_seconds"]
        return migrated

    @property
    def effective_hard_timeout_seconds(self) -> int:
        """返回有效单次硬上限；无参数，返回秒数，不抛异常。"""
        return self.hard_timeout_seconds

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        """校验命令名非空；参数为命令文本，返回去空格文本，空值时抛错。"""
        value = value.strip()
        if not value:
            raise ValueError("Agent command 不能为空")
        return value

    @field_validator(
        "runtime_config_hash",
        "runtime_config_content_hash",
        "runtime_config_resolved_hash",
    )
    @classmethod
    def validate_runtime_config_hash(cls, value: str | None) -> str | None:
        """校验运行配置摘要；参数为可选 SHA-256；返回原值；格式非法时抛错。"""
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("运行配置摘要必须是小写 SHA-256")
        return value

    _runtime_config_capability: _RuntimeConfigCapability | None = PrivateAttr(
        default=None
    )

    def with_internal_runtime_config(
        self, capability: _RuntimeConfigCapability
    ) -> AgentProfile:
        """冻结内部运行配置。

        参数为预检封印能力；返回带私有能力的 Profile 副本；能力或摘要不匹配时抛错。
        """
        if type(capability) is not _RuntimeConfigCapability or not capability.verify():
            raise ValueError("内部运行配置能力必须来自当前进程预检")
        if capability.hashes() != (
            self.runtime_config_hash,
            self.runtime_config_content_hash,
            self.runtime_config_resolved_hash,
        ):
            raise ValueError("内部运行配置能力与 Profile 摘要不匹配")
        frozen = self.model_copy()
        frozen._runtime_config_capability = capability
        return frozen

    def internal_runtime_config_capability(
        self,
    ) -> _RuntimeConfigCapability | None:
        """读取并验证内部能力；无参数；返回能力或 None；检测注入或篡改时抛错。"""
        private = self.__pydantic_private__ or {}
        if (
            "_runtime_config_content" in self.__dict__
            or "_runtime_config_content" in private
            or "_runtime_config_capability" in self.__dict__
        ):
            raise ValueError("检测到未授权运行配置能力注入")
        capability = private.get("_runtime_config_capability")
        if capability is None:
            return None
        if type(capability) is not _RuntimeConfigCapability or not capability.verify():
            raise ValueError("运行配置能力封印校验失败")
        if capability.hashes() != (
            self.runtime_config_hash,
            self.runtime_config_content_hash,
            self.runtime_config_resolved_hash,
        ):
            raise ValueError("运行配置能力与 Profile 摘要不匹配")
        return capability

    def internal_runtime_config_content(self) -> str | None:
        """读取并验证内部运行配置。

        无参数；返回当前进程私有内容或 None；发现旧私有字段注入、摘要缺失或不匹配
        时抛 ValueError。
        """
        capability = self.internal_runtime_config_capability()
        if capability is None:
            return None
        return capability.content()


def _default_profiles() -> dict[AgentName, AgentProfile]:
    """从内置目录创建默认 Agent 配置；无参数，返回内置 CLI 配置，不抛异常。"""
    return {
        definition.name: AgentProfile(command=definition.default_command)
        for definition in agent_definitions()
    }


class RunConfig(BaseModel):
    """保存一次 Debate Run 的冻结输入和安全预算。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path
    topic: str
    protocol_version: ReviewProtocolVersion = ReviewProtocolVersion.LEGACY_V0
    issue_ledger_enabled: bool = False
    counterexample_ballot: bool = False
    target_branch: str = "main"
    base_ref: str | None = None
    use_worktree: bool = False
    integrate_to_target: bool = True
    participants: tuple[AgentName, ...] = (
        AgentName.CODEX,
        AgentName.CLAUDE,
    )
    first_agent: AgentName = AgentName.CODEX
    max_turns: int = Field(default=8, gt=0)
    total_timeout_seconds: int = Field(default=7200, gt=0)
    max_process_failures: int = Field(default=2, gt=0)
    health_probe_timeout_seconds: int = Field(default=60, gt=0)
    circuit_breaker_threshold: int = Field(default=2, gt=0)
    circuit_breaker_window_seconds: int = Field(default=600, gt=0)
    circuit_breaker_cooldown_seconds: int = Field(default=300, gt=0)
    format_retries: int = Field(default=1, ge=0, le=1)
    stall_turns: int = Field(default=2, gt=0)
    state_root: Path = Field(
        default_factory=lambda: Path("agent/debate/.runtime").resolve()
    )
    setup_commands: tuple[tuple[str, ...], ...] = ()
    validation_commands: tuple[tuple[str, ...], ...] = ()
    allow_network: bool = False
    unrestricted_commands: bool = True
    realtime_output_enabled: bool = True
    raw_output_tail_lines: int = Field(default=500, gt=0, le=5000)
    invocation_event_limit: int = Field(default=1000, gt=0, le=10000)
    mcp_mode: Literal["minimal", "full"] = "minimal"
    environment_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
    proxy_environment_allowlist: tuple[str, ...] = ()
    codex_resume_sandbox_verified: bool = True
    allowed_scope: tuple[str, ...] = (".",)
    protected_scope: tuple[str, ...] = (
        ".git",
        ".agent/debate",
        ".agent/discuss",
        ".env",
    )
    high_impact_scope: tuple[str, ...] = ()
    facilitator_enabled: bool = True
    protocol_normalizer_enabled: bool = True
    facilitator_model: str = "deepseek-v4-flash"
    facilitator_base_url: str = "https://api.deepseek.com"
    facilitator_timeout_seconds: int = Field(default=120, gt=0)
    facilitator_api_key_env: str = "DEEPSEEK_API_KEY"
    profiles: dict[AgentName, AgentProfile] = Field(default_factory=_default_profiles)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_config(cls, value: Any) -> Any:
        """迁移历史冻结配置的缺失字段。

        参数为待校验配置；返回补齐 participants、protocol_version 和可选终局投票
        开关的载荷；
        非字典由 Pydantic 继续处理。旧 config.json 未记录协议版本时，显式固定为
        legacy-v0；非法或双字段配置继续由严格模型拒绝。
        """
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "protocol_version" not in migrated:
            migrated["protocol_version"] = ReviewProtocolVersion.LEGACY_V0.value
        if "counterexample_ballot" not in migrated:
            migrated["counterexample_ballot"] = False
        if "participants" not in migrated:
            profiles = migrated.get("profiles")
            if isinstance(profiles, dict):
                selected = [
                    agent.value
                    for agent in (AgentName.CODEX, AgentName.CLAUDE)
                    if agent in profiles or agent.value in profiles
                ]
                if len(selected) >= 2:
                    migrated["participants"] = selected
        return migrated

    @model_validator(mode="after")
    def validate_participants(self) -> RunConfig:
        """校验参与者合同；无参数，返回自身，人数、首位或 Profile 不合法时抛错。"""
        if len(self.participants) < 2:
            raise ValueError("讨论至少需要两个 CLI Agent")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("participants 不能包含重复 Agent")
        if self.first_agent not in self.participants:
            raise ValueError("first_agent 必须属于 participants")
        if (
            self.issue_ledger_enabled
            and self.protocol_version is not ReviewProtocolVersion.REVIEW_V1
        ):
            raise ValueError("Issue Ledger 仅支持 review-v1 协议")
        if (
            self.counterexample_ballot
            and self.protocol_version is not ReviewProtocolVersion.REVIEW_V1
        ):
            raise ValueError("反例式 Blind Ballot 仅支持 review-v1 协议")
        missing = [
            agent.value
            for agent in self.participants
            if agent not in self.profiles
        ]
        if missing:
            raise ValueError(f"参与 Agent 缺少 Profile: {', '.join(missing)}")
        return self

    @field_validator("workspace", "state_root")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        """规范化路径；参数为路径，返回绝对路径，解析失败时传播 OSError。"""
        return value.expanduser().resolve()

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: Path) -> Path:
        """校验工作空间目录；参数为绝对路径，返回原路径，无效时抛错。"""
        if not value.exists() or not value.is_dir():
            raise ValueError(f"workspace 不是有效目录: {value}")
        return value

    @field_validator("topic", "target_branch", "facilitator_model")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """校验关键文本；参数为文本，返回去空格文本，空值时抛错。"""
        value = value.strip()
        if not value:
            raise ValueError("关键配置文本不能为空")
        return value

    @field_validator("facilitator_base_url")
    @classmethod
    def validate_facilitator_base_url(cls, value: str) -> str:
        """校验 Facilitator URL；参数为 URL，返回规范值，非 HTTPS 时抛错。"""
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("Facilitator base URL 必须使用 HTTPS")
        return value

    @field_validator("facilitator_api_key_env")
    @classmethod
    def validate_facilitator_api_key_env(cls, value: str) -> str:
        """校验密钥环境变量名；参数为名称，返回原值，非法标识时抛错。"""
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", value) is None:
            raise ValueError("Facilitator API key 环境变量名非法")
        return value

    @field_validator("setup_commands", "validation_commands")
    @classmethod
    def validate_commands(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        """校验 argv 命令；参数为命令元组，返回原值，空命令时抛错。"""
        if any(
            not command or any(not part.strip() for part in command)
            for command in value
        ):
            raise ValueError("命令必须是非空 argv")
        return value

    @field_validator("proxy_environment_allowlist")
    @classmethod
    def validate_proxy_environment_allowlist(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        """校验代理和证书变量名。

        参数为待透传变量名；返回去重元组；包含非白名单名称时抛 ValueError。
        """
        allowed = {
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        invalid = [name for name in value if name not in allowed]
        if invalid:
            raise ValueError(f"代理或证书环境变量不在白名单: {', '.join(invalid)}")
        return tuple(dict.fromkeys(value))

    @field_validator("allowed_scope", "protected_scope", "high_impact_scope")
    @classmethod
    def validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """校验路径范围；参数为相对路径元组，返回规范去重值，越界时抛错。"""
        normalized: list[str] = []
        for item in value:
            path = PurePosixPath(item)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"范围必须是安全相对路径: {item}")
            text = path.as_posix().rstrip("/") or "."
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @classmethod
    def from_sources(
        cls,
        *,
        workspace: str | Path,
        topic: str | None = None,
        topic_file: str | Path | None = None,
        **overrides: Any,
    ) -> RunConfig:
        """从内联或文件目标构建配置；参数为路径和单一目标源，返回配置，冲突或读取失败时抛错。"""
        if topic is not None and topic_file is not None:
            raise ValueError("topic 与 topic_file 只能指定一种")
        if topic is None and topic_file is None:
            raise ValueError("必须提供 topic 或 topic_file")
        resolved_topic = topic
        if topic_file is not None:
            resolved_topic = Path(topic_file).expanduser().read_text(encoding="utf-8")
        assert resolved_topic is not None
        return cls(workspace=Path(workspace), topic=resolved_topic, **overrides)
