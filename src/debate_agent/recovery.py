"""根据 invocation journal、租约和 Git 事实恢复中断回合。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from debate_agent.artifacts import ArtifactStore
from debate_agent.git_workspace import GitWorkspace
from debate_agent.models import AgentName
from debate_agent.policy import PolicyViolationError, WritePolicy
from debate_agent.supervisor import LeaseConflictError, Supervisor
from debate_agent.validation import ValidationPhase, ValidationRunner


class RecoveryBlockedError(RuntimeError):
    """表示脏改动不能唯一、安全地归属到既有写前日志。"""


@dataclass(frozen=True, slots=True)
class RecoveredCheckpoint:
    """描述 Supervisor 已恢复并提交的单个中断写回合。"""

    commit_sha: str
    invocation_id: str
    agent: AgentName
    turn_id: str
    base_commit: str
    goal_contract_version: int
    changed_paths: tuple[str, ...]
    validation_passed: bool
    validation_report_ref: str


class RecoveryManager:
    """只恢复可唯一归属的脏树，不重放可能已执行的 Prompt。"""

    def __init__(
        self,
        *,
        workspace: GitWorkspace,
        artifacts: ArtifactStore,
        supervisor: Supervisor,
        write_policy: WritePolicy,
        validation_runner: ValidationRunner,
        validation_commands: Sequence[Sequence[str]],
        env: Mapping[str, str],
    ) -> None:
        """创建恢复器。

        参数为 Git/artifacts/Supervisor/策略/验证计划/环境；返回实例，不执行恢复。
        """
        self.workspace = workspace
        self.artifacts = artifacts
        self.supervisor = supervisor
        self.write_policy = write_policy
        self.validation_runner = validation_runner
        self.validation_commands = validation_commands
        self.env = env

    def recover_dirty_checkpoint(self) -> RecoveredCheckpoint | None:
        """恢复中断检查点。

        无参数；干净树返回 None，可归属时返回恢复记录；来源/策略/租约不明时抛阻断。
        """
        changed = self.workspace.changed_paths()
        if not changed:
            return None
        current_head = self.workspace.head()
        active_lease = self.supervisor.current_lease()
        if active_lease is None:
            raise RecoveryBlockedError("脏改动来源没有 active 写租约")
        candidates = [
            journal
            for journal in self.artifacts.recoverable_invocations()
            if journal.get("base_commit") == current_head
            and journal.get("agent") == active_lease.agent.value
            and journal.get("turn_id") == active_lease.turn_id
            and journal.get("goal_contract_version")
            == active_lease.goal_contract_version
        ]
        if len(candidates) != 1:
            raise RecoveryBlockedError("脏改动来源无法唯一确认")
        journal = candidates[0]
        try:
            agent = AgentName(str(journal["agent"]))
            turn_id = str(journal["turn_id"])
            version = int(journal["goal_contract_version"])
            lease = self.supervisor.assert_writer(agent)
            if (
                lease.turn_id != turn_id
                or lease.base_commit != current_head
                or lease.goal_contract_version != version
            ):
                raise RecoveryBlockedError("租约与写前日志不一致")
            revoked = self.supervisor.revoke_write_lease(turn_id)
            validated_paths = self.write_policy.validate_paths(changed)
        except (KeyError, ValueError, LeaseConflictError, PolicyViolationError) as exc:
            raise RecoveryBlockedError("脏改动来源或策略无法安全确认") from exc

        report = self.validation_runner.run(
            report_id=f"RECOVERED-{turn_id}",
            phase=ValidationPhase.CHECKPOINT,
            commands=self.validation_commands,
            cwd=self.workspace.worktree_path,
            env=self.env,
        )
        snapshot = self.workspace.snapshot()
        validation_status = (
            "recovered-passed" if report.passed else "recovered-failed-validation"
        )
        commit_sha = self.workspace.commit_checkpoint(
            agent=agent,
            turn_id=turn_id,
            goal_contract_version=version,
            validation_status=validation_status,
            summary="recover attributable interrupted turn",
            lease=revoked,
            validated_snapshot=snapshot,
            validated_paths=validated_paths,
            write_policy=self.write_policy,
        )
        if commit_sha is None:
            raise RecoveryBlockedError("恢复时脏改动意外消失")
        self.artifacts.mark_invocation_commit(
            str(journal["invocation_id"]), commit_sha=commit_sha
        )
        return RecoveredCheckpoint(
            commit_sha=commit_sha,
            invocation_id=str(journal["invocation_id"]),
            agent=agent,
            turn_id=turn_id,
            base_commit=current_head,
            goal_contract_version=version,
            changed_paths=tuple(validated_paths),
            validation_passed=report.passed,
            validation_report_ref=report.report_ref,
        )
