"""Debate Runtime 的中断恢复和非成功收尾阶段。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from debate_agent.artifacts import ArtifactError
from debate_agent.audit import (
    AuditBundleWriter,
)
from debate_agent.git_workspace import (
    GitWorkspace,
    GitWorkspaceError,
)
from debate_agent.graph import (
    GraphState,
)
from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    Decision,
    LeaseState,
    LeaseStatus,
    ReviewProtocolVersion,
    RunStatus,
    SessionHealth,
    TurnResult,
)
from debate_agent.recovery import (
    RecoveredCheckpoint,
    RecoveryBlockedError,
    RecoveryManager,
)
from debate_agent.runtime_completion import CompletionPhaseRuntime


class RecoveryPhaseRuntime(CompletionPhaseRuntime):
    """实现恢复归属、Session 重建和失败现场保留。"""

    def _node_recover_run(self, state: GraphState) -> Mapping[str, Any]:
        """恢复可归属脏树；参数为状态，返回当前 HEAD，来源不明时抛错。"""
        self._discard_non_success_audit(
            self._ensure_workspace(),
            expected_head=str(state.get("current_head") or "") or None,
        )
        self._ensure_components()
        assert self.supervisor is not None and self.validation_runner is not None
        recovered = RecoveryManager(
            workspace=self._ensure_workspace(),
            artifacts=self.artifacts,
            supervisor=self.supervisor,
            write_policy=self._write_policy(),
            validation_runner=self.validation_runner,
            validation_commands=self._contract().all_validation_commands,
            env=self.env,
        ).recover_dirty_checkpoint()
        validation_report_ref = None
        if (
            recovered is not None
            and self.config.protocol_version is ReviewProtocolVersion.REVIEW_V1
        ):
            report = self._review_head_validation(
                report_id=f"RECOVERED-HEAD-{recovered.turn_id}-{recovered.commit_sha[:12]}",
            )
            validation_report_ref = report.report_ref
        return {
            "current_head": self._ensure_workspace().head(),
            "last_commit": (
                recovered.commit_sha
                if recovered is not None
                else self._ensure_workspace().head()
            ),
            **(
                {"validation_report_ref": validation_report_ref}
                if validation_report_ref is not None
                else {}
            ),
        }

    def recover_before_resume(self, graph: Any) -> None:
        """把中断 Writer 的可归属脏树推进到提交后 checkpoint。

        参数为已绑定原 checkpointer 的图；无返回值；来源不明或恢复失败时抛错。
        """
        workspace = self._ensure_workspace()
        config = {
            "configurable": {"thread_id": self.run_id},
            "recursion_limit": 200,
        }
        snapshot = graph.get_state(config)
        state = cast(GraphState, dict(snapshot.values))
        self._discard_non_success_audit(
            workspace,
            expected_head=str(state.get("current_head") or "") or None,
        )
        if not workspace.changed_paths():
            self._revoke_clean_interrupted_lease()
            self._rebuild_clean_interrupted_sessions()
            return
        self._ensure_components()
        assert self.supervisor is not None and self.validation_runner is not None
        recovered = RecoveryManager(
            workspace=workspace,
            artifacts=self.artifacts,
            supervisor=self.supervisor,
            write_policy=self._write_policy(),
            validation_runner=self.validation_runner,
            validation_commands=self._contract().all_validation_commands,
            env=self.env,
        ).recover_dirty_checkpoint()
        if recovered is None:
            return
        validation_report_ref = recovered.validation_report_ref
        if self.config.protocol_version is ReviewProtocolVersion.REVIEW_V1:
            report = self._review_head_validation(
                report_id=f"RECOVERED-HEAD-{recovered.turn_id}-{recovered.commit_sha[:12]}",
            )
            validation_report_ref = report.report_ref
        turn = self._recovered_turn(recovered)
        lease = LeaseState.model_validate(state["lease"]).model_copy(
            update={"status": LeaseStatus.REVOKED}
        )
        turn_number = int(recovered.turn_id.removeprefix("TURN-"))
        graph.update_state(
            config,
            {
                "status": RunStatus.RUNNING.value,
                "stop_reason": None,
                "current_writer": recovered.agent.value,
                "current_turn": max(state.get("current_turn", 0), turn_number),
                "turn_result": turn.model_dump(mode="json"),
                "current_invocation_id": recovered.invocation_id,
                "lease": lease.model_dump(mode="json"),
                "validated_paths": list(recovered.changed_paths),
                "checkpoint_validation_passed": recovered.validation_passed,
                "validation_report_ref": validation_report_ref,
                "current_head": recovered.commit_sha,
                "last_commit": recovered.commit_sha,
                "made_checkpoint": True,
            },
            as_node="commit_checkpoint",
        )

    def _revoke_clean_interrupted_lease(self) -> None:
        """撤销干净树对应的中断写租约。

        参数：无。
        返回值：无；没有 active 租约时直接返回。
        异常：租约持久化损坏或撤销失败时传播，避免绕过单 Writer 门禁。
        """
        self._ensure_components()
        assert self.supervisor is not None
        active = self.supervisor.current_lease()
        if active is not None:
            self.supervisor.revoke_write_lease(active.turn_id)

    def _rebuild_clean_interrupted_sessions(self) -> None:
        """重建干净树上的中断 Session。

        无参数和返回值；中断日志身份非法时抛 ArtifactError，持久化失败时传播异常。
        """
        interrupted = [
            journal
            for journal in self.artifacts.recoverable_invocations()
            if journal.get("status") in {"prepared", "running"}
        ]
        if not interrupted:
            return
        self._ensure_components()
        assert self.sessions is not None
        rebuilt: set[AgentName] = set()
        for journal in interrupted:
            try:
                invocation_id = str(journal["invocation_id"])
                agent = AgentName(str(journal["agent"]))
            except (KeyError, ValueError) as exc:
                raise ArtifactError("中断调用日志身份非法") from exc
            self.artifacts.mark_invocation_failed(
                invocation_id,
                error="worker interrupted before durable result",
            )
            if agent not in rebuilt:
                self.sessions.rebuild(agent, health=SessionHealth.UNCERTAIN)
                rebuilt.add(agent)

    def _discard_non_success_audit(
        self,
        workspace: GitWorkspace,
        *,
        expected_head: str | None = None,
    ) -> None:
        """撤销可证明属于 Supervisor 的失败审计。

        参数为工作区和可选的持久化代码 HEAD；无返回值；异常时阻断恢复。
        """
        try:
            workspace.discard_supervisor_audit(
                AuditBundleWriter.expected_files,
                expected_head=expected_head,
            )
        except GitWorkspaceError as exc:
            raise RecoveryBlockedError("失败审计无法安全撤销") from exc

    def _recovered_turn(self, recovered: RecoveredCheckpoint) -> TurnResult:
        """还原或合成中断回合结果；参数为恢复记录，返回保守 TurnResult。"""
        journal = self.artifacts.read_invocation(recovered.invocation_id)
        structured_ref = journal.get("structured_ref")
        if structured_ref:
            result = self.artifacts.read_agent_result(str(structured_ref))
            if result.structured_output is not None:
                turn = TurnResult.model_validate(result.structured_output)
                if (
                    turn.agent is recovered.agent
                    and turn.turn_id == recovered.turn_id
                    and turn.base_commit == recovered.base_commit
                ):
                    return turn
        return TurnResult(
            agent=recovered.agent,
            turn_id=recovered.turn_id,
            base_commit=recovered.base_commit,
            observed_head=recovered.base_commit,
            goal_contract_version=recovered.goal_contract_version,
            decision=Decision.REVISE,
            summary="Supervisor 恢复并提交了可唯一归属的中断写入",
            accepted_points=[],
            issues=[],
            requested_scope_additions=[],
            requested_validation_additions=[],
            completion_assessment=CompletionAssessment.INCOMPLETE,
        )

    def _node_finalize_non_success(self, state: GraphState) -> Mapping[str, Any]:
        """生成非完成摘要并保留恢复资源；参数为状态，返回原终止状态。"""
        status = str(state.get("status", RunStatus.FAILED.value))
        reason = str(state.get("stop_reason") or "运行未完成")
        try:
            workspace = self._ensure_workspace()
            writer = AuditBundleWriter(
                worktree=workspace.worktree_path, run_id=self.run_id
            )
            writer.write_non_success(
                self._audit_input(state), status=status, stop_reason=reason
            )
            workspace.stage_audit_tree(writer.relative_path, require_only_audit=False)
        except (OSError, RuntimeError, ValueError) as exc:
            self.artifacts.write_diagnostic(
                phase="finalize_non_success",
                error=f"{type(exc).__name__}: {exc}",
            )
        self.artifacts.write_status(status, stop_reason=reason)
        return {"status": status, "stop_reason": reason}
