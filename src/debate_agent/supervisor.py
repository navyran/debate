"""确定性的单写者 Supervisor。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    LeaseState,
    LeaseStatus,
)


class LeaseConflictError(RuntimeError):
    """表示租约已被占用、身份不符或状态不允许。"""


class Supervisor:
    """管理写入租约与确定性换手，不参与业务裁决。"""

    def __init__(self, *, state_root: Path, run_id: str) -> None:
        """创建 Supervisor。

        参数为状态根和 run ID；初始化租约目录；文件错误时抛 OSError。
        """
        self.state_root = state_root.expanduser().resolve()
        self.run_id = run_id
        self.leases_dir = self.state_root / "runs" / run_id / "leases"
        self.leases_dir.mkdir(parents=True, exist_ok=True)
        self.invocations_dir = self.state_root / "runs" / run_id / "invocations"
        self.invocations_dir.mkdir(parents=True, exist_ok=True)
        self.active_lease_path = self.leases_dir / "active.json"
        self._active_lease = self._load_active_lease()

    def _load_active_lease(self) -> LeaseState | None:
        """加载现有租约；无参数，返回租约或 None，非法 JSON/模型时传播异常。"""
        if not self.active_lease_path.exists():
            return None
        return LeaseState.model_validate_json(
            self.active_lease_path.read_text(encoding="utf-8")
        )

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        """原子写入租约；参数为路径和文本，无返回值，文件错误时抛 OSError。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)

    def grant_write_lease(
        self,
        *,
        agent: AgentName,
        turn_id: str,
        base_commit: str,
        goal_contract_version: int,
    ) -> LeaseState:
        """发放唯一写入租约；参数为 Agent/回合/基线/契约，返回租约，冲突时抛错。"""
        if self._active_lease is not None or self.active_lease_path.exists():
            raise LeaseConflictError("已有 active 写入租约")
        prepared_path = self.invocations_dir / f"{turn_id}.json"
        prepared = {
            "run_id": self.run_id,
            "turn_id": turn_id,
            "agent": agent.value,
            "base_commit": base_commit,
            "goal_contract_version": goal_contract_version,
            "status": LeaseStatus.PREPARED.value,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write(
            prepared_path,
            json.dumps(prepared, ensure_ascii=False, indent=2),
        )
        lease = LeaseState(
            run_id=self.run_id,
            turn_id=turn_id,
            agent=agent,
            base_commit=base_commit,
            goal_contract_version=goal_contract_version,
            issued_at=datetime.now(timezone.utc),
            status=LeaseStatus.ACTIVE,
        )
        self._atomic_write(self.active_lease_path, lease.model_dump_json(indent=2))
        self._active_lease = lease
        return lease

    def assert_writer(self, agent: AgentName) -> LeaseState:
        """确认 Agent 持有租约；参数为 Agent，返回 active 租约，无权时抛错。"""
        lease = self._active_lease
        if (
            lease is None
            or lease.status is not LeaseStatus.ACTIVE
            or lease.agent is not agent
        ):
            raise LeaseConflictError(f"Agent {agent.value} 没有 active 写入租约")
        return lease

    def current_lease(self) -> LeaseState | None:
        """读取当前写租约；无参数，返回不可变模型或 None，不修改状态。"""
        return self._active_lease

    def revoke_write_lease(self, turn_id: str) -> LeaseState:
        """撤销并归档租约；参数为回合 ID，返回 revoked 租约，不匹配时抛错。"""
        lease = self._active_lease
        if lease is None or lease.turn_id != turn_id:
            raise LeaseConflictError(f"没有匹配的 active 租约: {turn_id}")
        revoked = lease.model_copy(update={"status": LeaseStatus.REVOKED})
        archive_path = self.leases_dir / f"{turn_id}.json"
        self._atomic_write(archive_path, revoked.model_dump_json(indent=2))
        self.active_lease_path.unlink(missing_ok=True)
        self._active_lease = None
        return revoked

    @staticmethod
    def assert_read_only_phase(
        *,
        before_fingerprint: str,
        after_fingerprint: str,
        phase: str,
    ) -> None:
        """检查只读阶段未写入。

        参数为前后工作树指纹与阶段名；无返回值；发生变化时抛 LeaseConflictError。
        """
        if before_fingerprint != after_fingerprint:
            raise LeaseConflictError(f"{phase} 只读阶段检测到未授权写入")

    @staticmethod
    def choose_next_writer(
        *,
        current_writer: AgentName,
        assessments: Mapping[AgentName, CompletionAssessment],
    ) -> AgentName:
        """选择下一 Writer；参数为当前作者和参与者判断，返回异议方，无异议时抛错。"""
        incomplete = [
            agent
            for agent, assessment in assessments.items()
            if assessment is CompletionAssessment.INCOMPLETE
        ]
        if not incomplete:
            raise ValueError("所有参与者均 complete，不应继续分配 Writer")
        if len(incomplete) == 1:
            return incomplete[0]
        participants = tuple(assessments)
        current_index = participants.index(current_writer)
        for offset in range(1, len(participants) + 1):
            candidate = participants[(current_index + offset) % len(participants)]
            if candidate in incomplete:
                return candidate
        raise ValueError("没有可分配的未完成参与者")
