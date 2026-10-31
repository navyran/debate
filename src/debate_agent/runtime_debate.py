"""Debate Runtime 的环境、Session、Writer、验证和检查点阶段。"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from debate_agent.artifacts import ArtifactError
from debate_agent.git_workspace import (
    GitWorkspace,
    WorkspaceRaceError,
    WorkspaceSnapshot,
)
from debate_agent.goal_contract import (
    GoalContractError,
)
from debate_agent.graph import (
    GraphState,
)
from debate_agent.memory import MemoryProtocolError
from debate_agent.models import (
    AgentName,
    Ballot,
    CompletionAssessment,
    FacilitatorRecord,
    FacilitatorStatus,
    LeaseState,
    RunStatus,
    TurnResult,
)
from debate_agent.policy import PolicyViolationError
from debate_agent.process import (
    ProcessRunner,
)
from debate_agent.prompts import (
    build_facilitator_instruction_prompt,
)
from debate_agent.runtime_base import RuntimeBase
from debate_agent.validation import (
    ValidationPhase,
)


class DebatePhaseRuntime(RuntimeBase):
    """实现准备环境到检查点交接的辩论阶段。"""

    def _node_preflight(self, state: GraphState) -> Mapping[str, Any]:
        """预检仓库/命令；参数为状态，返回 Git 基线字段，失败时抛错。"""
        if self.artifacts.abort_requested():
            return {
                "status": RunStatus.ABORTED.value,
                "stop_reason": "收到 abort 请求",
            }
        for agent in self.config.participants:
            profile = self.config.profiles[agent]
            if (
                shutil.which(profile.command, path=self.env.get("PATH")) is None
                and not Path(profile.command).is_file()
            ):
                raise FileNotFoundError(f"{agent.value} CLI 不可用: {profile.command}")
        base_ref = self.config.base_ref or self.config.target_branch
        self.workspace = GitWorkspace.preflight(
            self.config.workspace,
            target_branch=self.config.target_branch,
            base_ref=base_ref,
            state_root=self.config.state_root,
            run_id=self.run_id,
            use_worktree=self.config.use_worktree,
            excluded_paths=self.config.protected_scope,
        )
        self.artifacts.write_repo(self.workspace.to_record())
        return {
            "repo_root": str(self.workspace.repo_root),
            "target_branch": self.workspace.target_branch,
            "base_commit": self.workspace.base_commit,
            "current_head": self.workspace.base_commit,
            "worktree_path": str(self.workspace.worktree_path),
            "debate_branch": self.workspace.debate_branch,
        }

    def _node_acquire_repo_lock(self, state: GraphState) -> Mapping[str, Any]:
        """获取仓库锁；参数为状态，返回空更新，冲突时抛错。"""
        assert self.workspace is not None
        self.workspace.acquire_lock()
        self.artifacts.write_repo(self.workspace.to_record())
        return {}

    def _node_create_worktree(self, state: GraphState) -> Mapping[str, Any]:
        """创建共享 worktree；参数为状态，返回路径/HEAD，Git 失败时抛错。"""
        assert self.workspace is not None
        self.workspace.create_worktree()
        self.artifacts.write_repo(self.workspace.to_record())
        self._ensure_components()
        return {
            "worktree_path": str(self.workspace.worktree_path),
            "current_head": self.workspace.head(),
        }

    def _node_prepare_environment(self, state: GraphState) -> Mapping[str, Any]:
        """执行 setup 与基线验证；参数为状态，返回报告引用，失败时抛阻断。"""
        self._ensure_components()
        assert self.validation_runner is not None
        workspace = self._ensure_workspace()
        setup = self.validation_runner.run(
            report_id="SETUP",
            phase=ValidationPhase.SETUP,
            commands=self.config.setup_commands,
            cwd=workspace.worktree_path,
            env=self.env,
        )
        self.validation_runner.assert_phase_success(setup)
        baseline = self.validation_runner.run(
            report_id="BASELINE",
            phase=ValidationPhase.BASELINE,
            commands=self.config.validation_commands,
            cwd=workspace.worktree_path,
            env=self.env,
        )
        self.validation_runner.assert_phase_success(baseline)
        return {"validation_report_ref": baseline.report_ref}

    def _node_build_goal_contract(self, state: GraphState) -> Mapping[str, Any]:
        """创建初始 GoalContract；参数为状态，返回版本/引用，违规时抛错。"""
        try:
            contract = self.contract_store.load_latest()
        except GoalContractError:
            contract = self.contract_store.create(
                objective=self.config.topic,
                deliverables=("完成用户目标并交付工作空间变更",),
                acceptance_criteria=(
                    "所有参与 CLI Agent 都判断目标要求已完成",
                    "最终权威验证通过",
                ),
                baseline_validation_commands=self.config.validation_commands,
                allowed_scope=self.config.allowed_scope,
                protected_scope=self.config.protected_scope,
                immutable_constraints=(
                    "不得自动 push、发布或部署",
                    "Supervisor 不替 Agent 作业务裁决",
                    "只有全员 complete 且全员 final tree ack 才能 completed",
                ),
                command_policy=self.command_policy,
            )
        return {
            "goal_contract_version": contract.version,
            "goal_contract_ref": f"goal-contract/v{contract.version}.json",
        }

    def _node_ensure_sessions(self, state: GraphState) -> Mapping[str, Any]:
        """创建并确认参与者 Session；参数为状态，返回代次，写入时抛竞态。"""
        self._ensure_components()
        assert self.sessions is not None
        contract = self._contract()
        for agent in self.config.participants:
            self._sync_agent(agent)
            self.contract_store.confirm(agent, version=contract.version)
        self.contract_store.assert_both_confirmed(
            version=contract.version,
            participants=self.config.participants,
        )
        return {
            "session_generations": {
                agent.value: self.sessions.get(agent).generation
                for agent in self.config.participants
            }
        }

    def _node_sync_peer_memory(self, state: GraphState) -> Mapping[str, Any]:
        """同步下一 Writer 记忆；参数为状态，返回 pending 游标，失败时抛错。"""
        agent = AgentName(state.get("next_writer", AgentName.CODEX.value))
        self._sync_agent(agent)
        assert self.memory is not None
        return {
            "pending_update_refs": {
                item.value: self.memory.pending(item)
                for item in self.config.participants
            }
        }

    def _node_collect_change_ack(self, state: GraphState) -> Mapping[str, Any]:
        """检查 Writer 已确认当前 HEAD；参数为状态，返回空更新，未确认时抛错。"""
        self._ensure_components()
        assert self.sessions is not None
        agent = AgentName(state.get("next_writer", AgentName.CODEX.value))
        if self.sessions.get(agent).last_ack_commit != self._ensure_workspace().head():
            raise MemoryProtocolError("Writer 尚未确认当前 HEAD")
        return {}

    def _node_grant_write_lease(self, state: GraphState) -> Mapping[str, Any]:
        """发放唯一写租约；参数为状态，返回租约字典，冲突时抛错。"""
        self._ensure_components()
        assert self.supervisor is not None
        agent = AgentName(state.get("next_writer", AgentName.CODEX.value))
        turn_id = f"TURN-{state.get('current_turn', 0) + 1:03d}"
        lease = self.supervisor.grant_write_lease(
            agent=agent,
            turn_id=turn_id,
            base_commit=self._ensure_workspace().head(),
            goal_contract_version=self._contract().version,
        )
        return {"lease": lease.model_dump(mode="json")}

    def _facilitator_instruction_prompt(self, state: GraphState) -> str:
        """读取当前建议；参数为图状态，返回非授权 Prompt，引用无效时返回空文本。"""
        reference = state.get("facilitator_instruction_ref")
        if not reference:
            return ""
        path = Path(str(reference))
        if path.parent.as_posix() != "facilitator" or path.suffix != ".json":
            return ""
        payload = self.artifacts.read_facilitator_record(path.stem)
        if payload is None:
            return ""
        record = FacilitatorRecord.model_validate(payload)
        if (
            record.status is not FacilitatorStatus.DECISION
            or record.decision is None
            or record.head_sha != self._ensure_workspace().head()
        ):
            return ""
        return build_facilitator_instruction_prompt(
            head_sha=record.head_sha,
            instruction=record.decision.next_instruction,
            unresolved_criteria=record.decision.unresolved_criteria,
        )

    @staticmethod
    def _ballot_feedback_prompt(state: GraphState, agent: AgentName) -> str:
        """生成未完成票反馈；参数为状态/Agent，返回仅绑定该 Agent 上轮票据的文本。"""
        payload = state.get("ballots", {}).get(agent.value)
        if not isinstance(payload, dict):
            return ""
        try:
            ballot = Ballot.model_validate(payload)
        except ValidationError:
            return ""
        if ballot.completion is not CompletionAssessment.INCOMPLETE:
            return ""
        return (
            "你上一轮独立终审投了 incomplete，必须先核查并处理该反馈；"
            "若事实证明反馈不成立，也要在 TurnResult 中说明证据：\n"
            f"{ballot.rationale[:4000]}"
        )

    def _turn_prompt(self, *, agent: AgentName, turn_id: str, state: GraphState) -> str:
        """生成 Writer Prompt；参数为 Agent/turn，返回目标与权限约束文本。"""
        contract = self._contract()
        facilitator_prompt = self._facilitator_instruction_prompt(state)
        ballot_feedback = self._ballot_feedback_prompt(state, agent)
        return (
            f"你是 {agent.value}，当前获得唯一写入租约 {turn_id}。\n"
            f"当前 HEAD: {self._ensure_workspace().head()}\n"
            f"GoalContract:\n{contract.model_dump_json(indent=2)}\n\n"
            "请先自主读取 Git 和工作空间，再为完成原目标进行必要修改。"
            "禁止 git add/commit/reset/checkout/switch/merge/rebase/worktree/push，"
            "禁止后台 watcher/服务、部署、发布或范围外写入。可运行已批准命令。"
            "最终仅返回 TurnResult JSON，base_commit/observed_head "
            "绑定调用开始时 HEAD；"
            "requested_validation_additions 只能填写可机械执行的 argv 数组，"
            '例如 ["git","diff","--check"]；不得填写整段 shell 命令或'
            "自然语言检查说明，没有新增命令时返回空数组。"
            "completion_assessment 判断当前候选是否已满足业务目标并已准备好投"
            " complete 票。不要等待后续 Ballot 或 TreeAck，也不要把另一 Agent"
            " 尚未投票/确认当作 incomplete 的理由；这些门禁只会在双方过程评估"
            " complete 后由 Supervisor 另行触发。若当前实现和验证已满足"
            " GoalContract，就应现在标记 complete；Supervisor 仍会强制执行后续"
            " 双 Ballot 与双 TreeAck 后才允许结束。"
            + (f"\n\n{facilitator_prompt}" if facilitator_prompt else "")
            + (f"\n\n{ballot_feedback}" if ballot_feedback else "")
        )

    def _node_writer_turn(self, state: GraphState) -> Mapping[str, Any]:
        """调用 Writer；参数为状态，返回 TurnResult/调用 ID，失败时抛错。"""
        lease = LeaseState.model_validate(state["lease"])
        result = self._call_agent(
            agent=lease.agent,
            response_model=TurnResult,
            schema_name="turn.schema.json",
            prompt=self._turn_prompt(
                agent=lease.agent, turn_id=lease.turn_id, state=state
            ),
            phase="writer",
            turn_id=lease.turn_id,
            read_only=False,
        )
        if (
            result.turn_id != lease.turn_id
            or result.base_commit != lease.base_commit
            or result.observed_head != lease.base_commit
            or result.goal_contract_version != lease.goal_contract_version
        ):
            raise RuntimeError("Writer TurnResult 未绑定租约基线/GoalContract")
        invocation_id = self._last_invocation_id
        if not invocation_id or self.artifacts.find_invocation(invocation_id) is None:
            raise ArtifactError("Writer 实际 invocation ID 未记录")
        return {
            "turn_result": result.model_dump(mode="json"),
            "current_invocation_id": invocation_id,
        }

    def _node_quiesce_workspace(self, state: GraphState) -> Mapping[str, Any]:
        """撤销租约并等待静默；参数为状态，返回 revoked 租约/快照。"""
        self._ensure_components()
        assert self.supervisor is not None
        lease = LeaseState.model_validate(state["lease"])
        revoked = self.supervisor.revoke_write_lease(lease.turn_id)
        runner = ProcessRunner(raw_root=self.artifacts.run_dir / "raw")
        fingerprint = runner.wait_for_quiescence(
            fingerprint=lambda: self._ensure_workspace().snapshot().fingerprint,
            quiet_seconds=0.05,
            timeout_seconds=5,
            poll_seconds=0.02,
        )
        return {
            "lease": revoked.model_dump(mode="json"),
            "workspace_snapshot": {"fingerprint": fingerprint},
        }

    def _node_enforce_policy(self, state: GraphState) -> Mapping[str, Any]:
        """执行真实 diff 路径与 HEAD 策略；参数为状态，返回已验证路径。"""
        workspace = self._ensure_workspace()
        lease = LeaseState.model_validate(state["lease"])
        if workspace.head() != lease.base_commit:
            raise PolicyViolationError("Agent 执行了未授权 Git 状态变更")
        paths = self._write_policy().validate_paths(workspace.changed_paths())
        return {"validated_paths": paths}

    def _node_run_checkpoint_validation(self, state: GraphState) -> Mapping[str, Any]:
        """运行检查点权威验证；参数为状态，返回报告/快照，失败仍可提交。"""
        self._ensure_components()
        assert self.validation_runner is not None
        workspace = self._ensure_workspace()
        before_paths = tuple(workspace.changed_paths())
        lease = LeaseState.model_validate(state["lease"])
        report = self.validation_runner.run(
            report_id=lease.turn_id,
            phase=ValidationPhase.CHECKPOINT,
            commands=self._contract().all_validation_commands,
            cwd=workspace.worktree_path,
            env=self.env,
        )
        if tuple(workspace.changed_paths()) != before_paths:
            raise WorkspaceRaceError("权威验证命令修改了受控工作树")
        snapshot = workspace.snapshot()
        return {
            "checkpoint_validation_passed": report.passed,
            "validation_report_ref": report.report_ref,
            "workspace_snapshot": asdict(snapshot),
        }

    def _node_commit_checkpoint(self, state: GraphState) -> Mapping[str, Any]:
        """创建 Agent 检查点；参数为状态，返回 HEAD/提交标记，竞态时抛错。"""
        workspace = self._ensure_workspace()
        lease = LeaseState.model_validate(state["lease"])
        turn = TurnResult.model_validate(state["turn_result"])
        snapshot = WorkspaceSnapshot(**state["workspace_snapshot"])
        status = (
            "passed"
            if state.get("checkpoint_validation_passed", False)
            else "failed-validation"
        )
        commit = workspace.commit_checkpoint(
            agent=lease.agent,
            turn_id=lease.turn_id,
            goal_contract_version=lease.goal_contract_version,
            validation_status=status,
            summary=turn.summary,
            lease=lease,
            validated_snapshot=snapshot,
            validated_paths=state.get("validated_paths", []),
            write_policy=self._write_policy(),
        )
        head = commit or workspace.head()
        invocation_id = state.get("current_invocation_id")
        if commit and invocation_id:
            self.artifacts.mark_invocation_commit(invocation_id, commit_sha=commit)
        if commit:
            self._record_writer_ledger_addresses(
                writer=lease.agent,
                commit_sha=head,
                changed_paths=tuple(state.get("validated_paths", [])),
                checkpoint_validation_passed=bool(
                    state.get("checkpoint_validation_passed", False)
                ),
            )
        return {
            "current_head": head,
            "last_commit": head,
            "made_checkpoint": commit is not None,
        }
