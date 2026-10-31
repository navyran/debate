"""Agent 命令与文件写入策略。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PolicyViolationError(RuntimeError):
    """表示命令或路径违反 Supervisor 安全策略。"""


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """按可信模式或严格白名单校验 Agent 命令。"""

    approved_commands: tuple[tuple[str, ...], ...]
    environment_allowlist: frozenset[str]
    network_enabled: bool
    unrestricted_commands: bool

    def __init__(
        self,
        approved_commands: Iterable[Sequence[str]] = (),
        *,
        environment_allowlist: Iterable[str] = (),
        network_enabled: bool = False,
        unrestricted_commands: bool = False,
    ) -> None:
        """创建命令策略。

        参数为命令前缀、环境白名单、联网及完全授权开关；返回实例；
        空前缀时抛错。
        """
        normalized = tuple(
            tuple(part for part in command) for command in approved_commands
        )
        if any(
            not command or any(not part for part in command) for command in normalized
        ):
            raise ValueError("批准命令必须是非空 argv")
        object.__setattr__(self, "approved_commands", normalized)
        object.__setattr__(
            self,
            "environment_allowlist",
            frozenset(environment_allowlist),
        )
        object.__setattr__(self, "network_enabled", network_enabled)
        object.__setattr__(self, "unrestricted_commands", unrestricted_commands)

    def validate(self, argv: Sequence[str]) -> tuple[str, ...]:
        """校验命令 argv。

        参数为待执行命令；返回冻结 argv；违规时抛 PolicyViolationError。
        """
        command = tuple(argv)
        if not command or any(not part for part in command):
            raise PolicyViolationError("命令不能为空")
        if self.unrestricted_commands:
            return command
        if command[0] == "git":
            readonly = {"status", "diff", "show", "log", "rev-parse", "ls-files"}
            if len(command) < 2 or command[1] not in readonly:
                raise PolicyViolationError("禁止 Agent 执行 Git 状态变更")
            return command
        approved = any(
            command[: len(prefix)] == prefix for prefix in self.approved_commands
        )
        if not approved:
            raise PolicyViolationError(f"未批准命令: {command[0]}")
        network_commands = {"curl", "wget", "pip", "pip3", "npm", "pnpm", "yarn"}
        if command[0] in network_commands and not self.network_enabled:
            raise PolicyViolationError(f"未开启联网权限: {command[0]}")
        return command

    def filter_environment(self, source: Mapping[str, str]) -> dict[str, str]:
        """筛选子进程环境。

        参数为来源环境映射；返回仅含白名单键的新字典；不修改输入且不抛异常。
        """
        return {
            key: value
            for key, value in source.items()
            if key in self.environment_allowlist
        }

    def claude_allowed_tools(self) -> tuple[str, ...]:
        """编译 Claude 工具权限；无参数，返回完全授权或严格白名单规则。"""
        if self.unrestricted_commands:
            return ("Read", "Edit", "Write", "Glob", "Grep", "Bash")
        rules = [
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash(git status *)",
            "Bash(git diff *)",
            "Bash(git show *)",
            "Bash(git log *)",
        ]
        rules.extend(
            f"Bash({' '.join(command)} *)" for command in self.approved_commands
        )
        return tuple(rules)


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """校验所有变更路径均位于 worktree 和 GoalContract 范围内。"""

    worktree: Path
    allowed_scope: tuple[str, ...]
    protected_scope: tuple[str, ...]
    high_impact_scope: tuple[str, ...]

    def __init__(
        self,
        *,
        worktree: Path,
        allowed_scope: Iterable[str],
        protected_scope: Iterable[str] = (),
        high_impact_scope: Iterable[str] = (),
    ) -> None:
        """创建路径策略；参数为 worktree 和范围，返回实例，无效根目录时抛错。"""
        root = worktree.expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"worktree 不是目录: {root}")
        object.__setattr__(self, "worktree", root)
        object.__setattr__(
            self,
            "allowed_scope",
            tuple(self._normalize_scope(p) for p in allowed_scope),
        )
        object.__setattr__(
            self,
            "protected_scope",
            tuple(self._normalize_scope(p) for p in protected_scope),
        )
        object.__setattr__(
            self,
            "high_impact_scope",
            tuple(self._normalize_scope(p) for p in high_impact_scope),
        )

    @staticmethod
    def _normalize_scope(value: str) -> str:
        """规范化范围；参数为相对路径，返回 POSIX 路径，绝对或上跳路径时抛错。"""
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"范围必须是安全相对路径: {value}")
        normalized = path.as_posix().rstrip("/")
        return normalized or "."

    @staticmethod
    def _within_scope(path: str, scope: str) -> bool:
        """判断路径是否属于范围；参数为两个 POSIX 路径，返回布尔值，不抛异常。"""
        return scope == "." or path == scope or path.startswith(scope + "/")

    def _validate_one(self, value: str) -> str:
        """校验单个变更路径；参数为相对路径，返回规范路径，越界时抛策略异常。"""
        relative = self._normalize_scope(value)
        parts = PurePosixPath(relative).parts
        if ".git" in parts:
            raise PolicyViolationError(f"禁止修改 Git 元数据: {relative}")
        if any(
            self._within_scope(relative, audit_root)
            for audit_root in (".agent/debate", ".agent/discuss")
        ):
            raise PolicyViolationError(f"Agent 禁止修改审计目录: {relative}")
        sensitive_names = {".env", "id_rsa", "id_ed25519"}
        if any(
            part in sensitive_names
            or part.startswith(".env.")
            or part.endswith((".pem", ".key"))
            for part in parts
        ):
            raise PolicyViolationError(f"禁止修改敏感路径: {relative}")
        if any(self._within_scope(relative, scope) for scope in self.protected_scope):
            raise PolicyViolationError(f"路径属于保护范围: {relative}")
        if not any(self._within_scope(relative, scope) for scope in self.allowed_scope):
            raise PolicyViolationError(f"路径不在允许范围: {relative}")
        high_impact_defaults = (
            ".github",
            "Dockerfile",
            "pyproject.toml",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "uv.lock",
        )
        is_high_impact = any(
            self._within_scope(relative, scope) for scope in high_impact_defaults
        )
        explicitly_allowed = any(
            self._within_scope(relative, scope) for scope in self.high_impact_scope
        )
        if is_high_impact and not explicitly_allowed:
            raise PolicyViolationError(f"高影响路径未被显式允许: {relative}")

        candidate = (self.worktree / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.worktree):
            raise PolicyViolationError(f"路径指向 worktree 之外: {relative}")
        current = candidate if candidate.is_dir() else candidate.parent
        while current != self.worktree:
            if (current / ".git").exists():
                raise PolicyViolationError(f"路径属于嵌套仓库: {relative}")
            current = current.parent
        return relative

    def validate_paths(self, paths: Iterable[str]) -> list[str]:
        """校验变更路径集合；参数为相对路径迭代器，返回去重排序列表，违规时抛错。"""
        return sorted({self._validate_one(path) for path in paths})
