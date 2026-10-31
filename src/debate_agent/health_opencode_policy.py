"""OpenCode 配置、权限、MCP 与协议解析规则。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from debate_agent.adapters import (
    scan_opencode_jsonl,
)
from debate_agent.health_models import (
    ANSI_ESCAPE_PATTERN,
    OPENCODE_MCP_LIST_ENTRY_PATTERN,
    OPENCODE_MCP_LIST_MAX_BYTES,
    OPENCODE_MCP_LIST_TOTAL_PATTERN,
    OPENCODE_MCP_MAX_COUNT,
    OPENCODE_RESOLVED_CONFIG_MAX_BYTES,
    CliCommandResult,
    _OpenCodeMcpListSummary,
)


class OpenCodePolicySupport:
    """集中保存 OpenCode 健康策略的纯解析与安全判断。"""

    @staticmethod
    def _config_content(
        disabled_mcp_names: Sequence[str] | None = None,
    ) -> str:
        """生成安全配置；参数为需禁用的 MCP 名称；返回规范 JSON；不抛异常。"""
        payload: dict[str, object] = {
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
        }
        if disabled_mcp_names is not None:
            payload["mcp"] = {name: {"enabled": False} for name in disabled_mcp_names}
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _controlled_environment(
        cls, base: Mapping[str, str], *, config_content: str
    ) -> dict[str, str]:
        """合并受控覆盖；参数为基础环境/安全配置；返回副本；不修改源映射。"""
        environment = dict(base)
        environment.update(
            {
                "OPENCODE_CONFIG_CONTENT": config_content,
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            }
        )
        return environment

    def initial_environment(
        self, *, base: Mapping[str, str], mcp_mode: str
    ) -> Mapping[str, str]:
        """准备 OpenCode 初始环境；参数为基础环境/MCP 模式；返回新映射。"""
        return self._controlled_environment(base, config_content=self._config_content())

    @staticmethod
    def _resolved_config(result: CliCommandResult) -> dict[str, object] | None:
        """解析 resolved config；参数为命令结果；返回对象或 None；不回显原文。"""
        if result.return_code != 0:
            return None
        try:
            if len(result.stdout.encode("utf-8")) > OPENCODE_RESOLVED_CONFIG_MAX_BYTES:
                return None
            payload = json.loads(result.stdout)
        except (UnicodeEncodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not OpenCodePolicySupport._utf8_safe(
            payload
        ):
            return None
        return payload

    @staticmethod
    def _utf8_safe(value: object) -> bool:
        """验证 JSON 值可按 UTF-8 表示；参数为值；返回布尔值；不回显原值。"""
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return False
            return True
        if isinstance(value, Mapping):
            return all(
                isinstance(key, str)
                and OpenCodePolicySupport._utf8_safe(key)
                and OpenCodePolicySupport._utf8_safe(item)
                for key, item in value.items()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return all(OpenCodePolicySupport._utf8_safe(item) for item in value)
        return True

    @staticmethod
    def _model(value: object) -> str | None:
        """校验 provider/model；参数为候选值；返回原值或 None；不抛异常。"""
        if not isinstance(value, str):
            return None
        return value if re.fullmatch(r"[^\s/]+/[^\s/]+", value) else None

    @staticmethod
    def _provider_authenticated(result: CliCommandResult, provider: str) -> bool:
        """验证目标 provider 已认证；参数为结果/provider；返回布尔值；不回显输出。"""
        if result.return_code != 0:
            return False
        credential_pattern = re.compile(
            r"^\s*(?:│\s*)?[ℹ●◆◇]\s+(.+?)\s+"
            r"(api|oauth|wellknown)\s*$",
            flags=re.IGNORECASE,
        )
        clean_output = ANSI_ESCAPE_PATTERN.sub("", result.stdout)

        def normalized_label(value: str) -> str:
            """规范 provider 标签；参数为显示名；返回仅字母数字的小写值。"""
            return re.sub(r"[^a-z0-9]+", "", value.casefold())

        expected = normalized_label(provider)
        for line in clean_output.splitlines():
            match = credential_pattern.fullmatch(line)
            if match is not None and normalized_label(match.group(1)) == expected:
                return True
        return False

    @staticmethod
    def _provider_configured(
        resolved_config: Mapping[str, object], provider: str
    ) -> bool:
        """验证目标 provider 存在于已解析配置；参数为配置/provider；返回布尔值。"""
        configured = resolved_config.get("provider")
        if not isinstance(configured, Mapping):
            return False
        expected = provider.casefold()
        return any(
            isinstance(name, str) and name.casefold() == expected for name in configured
        )

    @staticmethod
    def _permission_allows(value: object) -> bool:
        """验证全局权限允许；参数为 resolved permission；返回布尔值；不抛异常。"""
        if value == "allow":
            return True
        return isinstance(value, dict) and value.get("*") == "allow"

    @staticmethod
    def _contains_deny(value: object) -> bool:
        """递归查找显式 deny；参数为权限值；返回是否命中；不抛异常。"""
        if isinstance(value, str):
            return value.casefold() == "deny"
        if isinstance(value, Mapping):
            return any(
                OpenCodePolicySupport._contains_deny(item) for item in value.values()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return any(OpenCodePolicySupport._contains_deny(item) for item in value)
        return False

    @classmethod
    def _permission_valid(cls, payload: Mapping[str, object]) -> bool:
        """验证顶层与 build 权限；参数为 resolved 对象；返回布尔值；不抛异常。"""
        top_permission = payload.get("permission")
        if not cls._permission_allows(top_permission) or cls._contains_deny(
            top_permission
        ):
            return False
        agent = payload.get("agent")
        if not isinstance(agent, Mapping):
            return True
        build = agent.get("build")
        if not isinstance(build, Mapping) or "permission" not in build:
            return True
        return not cls._contains_deny(build.get("permission"))

    @staticmethod
    def _safety_switches_valid(payload: Mapping[str, object]) -> bool:
        """验证共享/快照/升级开关；参数为 resolved 对象；返回布尔值。"""
        return (
            payload.get("share") == "disabled"
            and payload.get("snapshot") is False
            and payload.get("autoupdate") is False
        )

    @staticmethod
    def _mcp_names(payload: Mapping[str, object]) -> tuple[str, ...] | None:
        """提取 MCP 名称；参数为 resolved 对象；返回名称或 None；不持久化原对象。"""
        mcp = payload.get("mcp", {})
        if not isinstance(mcp, Mapping) or len(mcp) > OPENCODE_MCP_MAX_COUNT:
            return None
        names = tuple(mcp)
        if any(
            not isinstance(name, str)
            or not name.strip()
            or not OpenCodePolicySupport._utf8_safe(name)
            for name in names
        ):
            return None
        return names

    @staticmethod
    def _mcp_enabled_count(payload: Mapping[str, object]) -> int | None:
        """计算 MCP 启用数。

        参数为 resolved 对象；返回计数或 None；缺 enabled 视为启用。
        """
        names = OpenCodePolicySupport._mcp_names(payload)
        mcp = payload.get("mcp", {})
        if names is None or not isinstance(mcp, Mapping):
            return None
        return sum(
            1
            for name in names
            if not (
                isinstance(mcp[name], Mapping) and mcp[name].get("enabled") is False
            )
        )

    @staticmethod
    def _mcp_names_hash(names: Sequence[str]) -> str | None:
        """摘要 MCP 名称；参数为名称序列；返回排序 SHA-256 或 None；不回显原值。"""
        if not all(OpenCodePolicySupport._utf8_safe(name) for name in names):
            return None
        try:
            serialized = json.dumps(
                sorted(names), ensure_ascii=False, separators=(",", ":")
            )
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        except (TypeError, UnicodeEncodeError):
            return None

    @classmethod
    def _mcp_list_summary(
        cls, result: CliCommandResult
    ) -> _OpenCodeMcpListSummary | None:
        """解析 MCP list 聚合信息；参数为命令结果；返回摘要或 None；不回显原文。"""
        if result.return_code != 0:
            return None
        try:
            if len(result.stdout.encode("utf-8")) > OPENCODE_MCP_LIST_MAX_BYTES:
                return None
        except UnicodeEncodeError:
            return None
        output = ANSI_ESCAPE_PATTERN.sub("", result.stdout)
        if "No MCP servers configured" in output:
            return _OpenCodeMcpListSummary(
                total_count=0,
                enabled_count=0,
                healthy_count=0,
                status="healthy",
                names_hash=hashlib.sha256(b"[]").hexdigest(),
            )
        names: list[str] = []
        statuses: list[str] = []
        declared_count: int | None = None
        expected_icons = {
            "connected": "✓",
            "disabled": "○",
            "failed": "✗",
            "needs authentication": "⚠",
            "needs client registration": "✗",
            "not initialized": "○",
        }
        for line in output.splitlines():
            entry = OPENCODE_MCP_LIST_ENTRY_PATTERN.fullmatch(line)
            if entry is not None:
                name = entry.group("name")
                status = entry.group("status")
                if (
                    not name.strip()
                    or not cls._utf8_safe(name)
                    or entry.group("icon") != expected_icons[status]
                ):
                    return None
                names.append(name)
                statuses.append(status)
                continue
            total = OPENCODE_MCP_LIST_TOTAL_PATTERN.fullmatch(line)
            if total is not None:
                if declared_count is not None:
                    return None
                declared_count = int(total.group("count"))
        if (
            declared_count is None
            or declared_count != len(names)
            or declared_count > OPENCODE_MCP_MAX_COUNT
        ):
            return None
        names_hash = cls._mcp_names_hash(names)
        if names_hash is None:
            return None
        enabled_count = sum(status != "disabled" for status in statuses)
        healthy_count = sum(status == "connected" for status in statuses)
        status = (
            "healthy"
            if all(item in {"connected", "disabled"} for item in statuses)
            else "degraded"
        )
        return _OpenCodeMcpListSummary(
            total_count=declared_count,
            enabled_count=enabled_count,
            healthy_count=healthy_count,
            status=status,
            names_hash=names_hash,
        )

    @staticmethod
    def _resolved_config_hash(payload: Mapping[str, object]) -> str | None:
        """摘要最终 resolved config；参数为配置；返回 SHA-256 或 None；不回显原配置。"""
        if not OpenCodePolicySupport._utf8_safe(payload):
            return None
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (TypeError, UnicodeEncodeError):
            return None

    @staticmethod
    def _probe_protocol(
        stdout: str,
    ) -> tuple[tuple[str, ...], str | None, bool, bool, bool]:
        """扫描 OpenCode JSONL；参数为输出；返回 Session/text/error/wait/invalid。"""
        state = scan_opencode_jsonl(stdout.splitlines())
        return (
            state.session_ids,
            state.last_text,
            state.error_detail is not None,
            state.interaction_wait,
            state.invalid_json,
        )
