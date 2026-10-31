"""不可弱化、仅可增量演进的 GoalContract。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from debate_agent.models import AgentName
from debate_agent.policy import CommandPolicy

_EXECUTABLE_TOKEN = re.compile(r"[A-Za-z0-9_./\\:+-]+")


class GoalContractError(RuntimeError):
    """表示目标契约确认、范围或演进违反不可弱化规则。"""


class GoalContract(BaseModel):
    """保存所有参与者完成判断的版本化固定基准。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    objective: str
    deliverables: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    baseline_validation_commands: tuple[tuple[str, ...], ...]
    added_validation_commands: tuple[tuple[str, ...], ...] = ()
    allowed_scope: tuple[str, ...]
    protected_scope: tuple[str, ...]
    derived_scope: tuple[str, ...] = ()
    immutable_constraints: tuple[str, ...]

    @model_validator(mode="after")
    def validate_required_content(self) -> GoalContract:
        """校验契约必填内容；无参数，返回自身，空目标或关键集合时抛错。"""
        if not self.objective.strip():
            raise ValueError("objective 不能为空")
        required = {
            "deliverables": self.deliverables,
            "acceptance_criteria": self.acceptance_criteria,
            "allowed_scope": self.allowed_scope,
            "immutable_constraints": self.immutable_constraints,
        }
        empty = [name for name, values in required.items() if not values]
        if empty:
            raise ValueError(f"GoalContract 关键集合不能为空: {', '.join(empty)}")
        return self

    @property
    def effective_scope(self) -> tuple[str, ...]:
        """计算有效范围；无参数，返回初始与派生范围的稳定去重元组。"""
        return tuple(dict.fromkeys((*self.allowed_scope, *self.derived_scope)))

    @property
    def all_validation_commands(self) -> tuple[tuple[str, ...], ...]:
        """计算完整验证计划；无参数，返回基线加新增命令，不抛异常。"""
        return tuple(
            dict.fromkeys(
                (*self.baseline_validation_commands, *self.added_validation_commands)
            )
        )


class GoalContractStore:
    """持久化 GoalContract 版本和参与者确认记录。"""

    def __init__(self, *, state_root: Path, run_id: str) -> None:
        """创建契约存储；参数为状态根和 run ID，返回实例，目录失败时抛错。"""
        self.root = (
            state_root.expanduser().resolve() / "runs" / run_id / "goal-contract"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.confirmations_path = self.root / "confirmations.json"

    @staticmethod
    def _normalize_scope(values: Iterable[str]) -> tuple[str, ...]:
        """规范化相对范围；参数为路径文本，返回去重元组，越界路径时抛错。"""
        normalized: list[str] = []
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise GoalContractError(f"范围必须是安全相对路径: {value}")
            text = path.as_posix().rstrip("/") or "."
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @staticmethod
    def _commands(
        commands: Sequence[Sequence[str]], command_policy: CommandPolicy
    ) -> tuple[tuple[str, ...], ...]:
        """验证命令计划；参数为 argv 集合和策略，返回冻结去重命令，违规时抛错。"""
        validated: list[tuple[str, ...]] = []
        for command in commands:
            argv = tuple(command)
            if not argv or any(not part for part in argv):
                raise GoalContractError("验证命令必须是非空 argv")
            executable = argv[0]
            if (
                _EXECUTABLE_TOKEN.fullmatch(executable) is None
                and not Path(executable).is_file()
            ):
                raise GoalContractError(
                    "验证命令必须使用拆分 argv，首项必须是可执行命令"
                )
            validated.append(command_policy.validate(argv))
        return tuple(dict.fromkeys(validated))

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        """原子写 JSON；参数为路径和载荷，无返回值，序列化或文件失败时抛错。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _persist(self, contract: GoalContract) -> Path:
        """保存不可覆盖版本；参数为契约，返回路径，版本已存在时抛错。"""
        path = self.root / f"v{contract.version}.json"
        if path.exists():
            raise GoalContractError(f"GoalContract v{contract.version} 已存在")
        self._atomic_json(path, contract.model_dump(mode="json"))
        self._atomic_json(
            self.confirmations_path,
            {"version": contract.version, "confirmed_by": []},
        )
        return path

    def create(
        self,
        *,
        objective: str,
        deliverables: Sequence[str],
        acceptance_criteria: Sequence[str],
        baseline_validation_commands: Sequence[Sequence[str]],
        allowed_scope: Sequence[str],
        protected_scope: Sequence[str],
        immutable_constraints: Sequence[str],
        command_policy: CommandPolicy,
    ) -> GoalContract:
        """创建初始契约。

        参数为目标、交付物、验收、验证、范围、约束和策略；返回 v1；违规时抛错。
        """
        contract = GoalContract(
            version=1,
            objective=objective.strip(),
            deliverables=tuple(dict.fromkeys(deliverables)),
            acceptance_criteria=tuple(dict.fromkeys(acceptance_criteria)),
            baseline_validation_commands=self._commands(
                baseline_validation_commands, command_policy
            ),
            allowed_scope=self._normalize_scope(allowed_scope),
            protected_scope=self._normalize_scope(protected_scope),
            immutable_constraints=tuple(dict.fromkeys(immutable_constraints)),
        )
        self._persist(contract)
        return contract

    def load(self, version: int) -> GoalContract:
        """读取指定契约；参数为版本，返回 GoalContract，文件/模型错误时传播异常。"""
        path = self.root / f"v{version}.json"
        return GoalContract.model_validate_json(path.read_text(encoding="utf-8"))

    def load_latest(self) -> GoalContract:
        """读取最新契约；无参数，返回最高版本，尚无契约时抛 GoalContractError。"""
        versions: list[tuple[int, Path]] = []
        for path in self.root.glob("v*.json"):
            match = path.stem.removeprefix("v")
            if match.isdigit():
                versions.append((int(match), path))
        if not versions:
            raise GoalContractError("GoalContract 尚未创建")
        return GoalContract.model_validate_json(
            max(versions, key=lambda item: item[0])[1].read_text(encoding="utf-8")
        )

    def evolve(
        self,
        current: GoalContract,
        *,
        acceptance_additions: Sequence[str],
        validation_additions: Sequence[Sequence[str]],
        derived_scope_additions: Sequence[str],
        scope_approvals: set[AgentName],
        command_policy: CommandPolicy,
        required_agents: set[AgentName] | None = None,
    ) -> GoalContract:
        """增量演进契约。

        参数为当前版本和新增验收/验证/派生范围；返回下一版本；范围未获双方同意时抛错。
        """
        derived = self._normalize_scope(derived_scope_additions)
        required = required_agents or {AgentName.CODEX, AgentName.CLAUDE}
        if derived and scope_approvals != required:
            raise GoalContractError("双方一致规则要求所有参与 Agent 批准派生范围")
        validations = self._commands(validation_additions, command_policy)
        evolved = current.model_copy(
            update={
                "version": current.version + 1,
                "acceptance_criteria": tuple(
                    dict.fromkeys((*current.acceptance_criteria, *acceptance_additions))
                ),
                "added_validation_commands": tuple(
                    dict.fromkeys((*current.added_validation_commands, *validations))
                ),
                "derived_scope": tuple(
                    dict.fromkeys((*current.derived_scope, *derived))
                ),
            }
        )
        self._persist(evolved)
        return evolved

    def confirm(self, agent: AgentName, *, version: int) -> None:
        """记录 Agent 确认；参数为 Agent 和版本，无返回值，版本不符时抛错。"""
        if not self.confirmations_path.exists():
            raise GoalContractError("GoalContract 尚未创建")
        payload = json.loads(self.confirmations_path.read_text(encoding="utf-8"))
        if payload["version"] != version:
            raise GoalContractError("确认版本与当前 GoalContract 不一致")
        confirmed: list[str] = payload["confirmed_by"]
        if agent.value not in confirmed:
            confirmed.append(agent.value)
        self._atomic_json(self.confirmations_path, payload)

    def assert_both_confirmed(
        self,
        *,
        version: int,
        participants: tuple[AgentName, ...] = (
            AgentName.CODEX,
            AgentName.CLAUDE,
        ),
    ) -> None:
        """检查所有参与者确认；参数为版本/参与者，无返回值，缺确认时抛错。"""
        if not self.confirmations_path.exists():
            raise GoalContractError("GoalContract 尚未创建")
        payload = json.loads(self.confirmations_path.read_text(encoding="utf-8"))
        if payload["version"] != version or set(payload["confirmed_by"]) != {
            agent.value for agent in participants
        }:
            raise GoalContractError("当前 GoalContract 尚未获得双方/所有参与者确认")

    @classmethod
    def assert_requested_paths_allowed(
        cls,
        *,
        allowed_scope: Sequence[str],
        derived_scope: Sequence[str],
        requested_paths: Sequence[str],
    ) -> None:
        """检查请求路径未扩展目标。

        参数为初始/派生范围和请求路径；无返回值；范围外路径时抛 GoalContractError。
        """
        scopes = cls._normalize_scope((*allowed_scope, *derived_scope))
        requested = cls._normalize_scope(requested_paths)
        for path in requested:
            if not any(
                scope == "." or path == scope or path.startswith(scope + "/")
                for scope in scopes
            ):
                raise GoalContractError(f"请求路径构成目标扩展: {path}")
