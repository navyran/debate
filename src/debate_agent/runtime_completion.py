"""Debate Runtime 的最终验证、共识、审计和集成阶段。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from debate_agent.agent_catalog import agent_definition
from debate_agent.audit import (
    AgentAuditMetadata,
    AuditBundleInput,
    AuditBundleWriter,
    DecisionRecord,
    FinalTreeCandidate,
    FinalTreeCoordinator,
    FinalTreeError,
)
from debate_agent.git_workspace import (
    IntegrationBlockedError,
    WorkspaceRaceError,
)
from debate_agent.graph import (
    GraphState,
    collect_blind_ballots,
)
from debate_agent.issue_ledger import (
    IssueStatus,
    LedgerIssue,
)
from debate_agent.models import (
    AgentName,
    AgentResult,
    Ballot,
    CompletionAssessment,
    FacilitatorRecord,
    FacilitatorStatus,
    RunStatus,
    TreeAck,
    TurnResult,
)
from debate_agent.paths import select_audit_relative_path
from debate_agent.process import (
    PromptTransport,
)
from debate_agent.runtime_review import ReviewPhaseRuntime
from debate_agent.validation import (
    ValidationPhase,
    ValidationReport,
)


class CompletionPhaseRuntime(ReviewPhaseRuntime):
    """实现 Ballot、审计、TreeAck 和最终集成。"""

    def _node_run_final_validation(self, state: GraphState) -> Mapping[str, Any]:
        """运行完整最终验证；参数为状态，返回 passed/报告引用。"""
        self._ensure_components()
        assert self.validation_runner is not None
        workspace = self._ensure_workspace()
        head = workspace.head()
        before = workspace.snapshot().fingerprint
        report = self.validation_runner.run(
            report_id=f"FINAL-{state.get('current_turn', 0)}",
            phase=ValidationPhase.FINAL,
            commands=self._contract().all_validation_commands,
            cwd=workspace.worktree_path,
            env=self.env,
            head_sha=head,
        )
        if workspace.snapshot().fingerprint != before or workspace.head() != head:
            raise WorkspaceRaceError("最终权威验证命令修改了受控工作树或 HEAD")
        if report.head_sha != head:
            raise RuntimeError("最终验证报告未绑定实际 current HEAD")
        return {
            "final_validation_passed": report.passed,
            "validation_report_ref": report.report_ref,
        }

    def _ballot_prompt(self, agent: AgentName, head: str) -> str:
        """生成终审 Prompt；参数为 Agent/HEAD，返回严格只读文本。"""
        prompt = (
            f"你是 {agent.value}，对固定代码候选独立终审。严格只读。\n"
            f"CURRENT_HEAD={head}\nGOAL_VERSION={self._contract().version}\n"
            f"GoalContract:\n{self._contract().model_dump_json(indent=2)}\n"
            "请自主读取代码和权威验证报告；不要猜测另一 Agent 的投票。"
            "返回 Ballot JSON，只有全部目标完成才 completion=complete。"
        )
        unresolved = self._unresolved_blocking_ledger_issues()
        if unresolved:
            prompt += (
                "\n当前存在未关闭的阻断 Ledger Issue，必须投 completion=incomplete：\n"
                + "\n".join(
                    f"- {issue.issue_id}: {issue.claim}" for issue in unresolved
                )
            )
        if self.config.counterexample_ballot:
            prompt += (
                "\n反例式终局投票已显式启用：除既有字段外，必须填写非空的"
                " strongest_counterexample（最强反例）和 counterexample_resolution"
                "（对该反例的处理结论及可核查证据）。"
            )
        return prompt

    def _unresolved_blocking_ledger_issues(self) -> tuple[LedgerIssue, ...]:
        """读取终局投票不得越过的阻断账本项。

        无参数；返回当前唯一 Ledger 中仍为 OPEN 且 blocking 的 Issue；未启用 Ledger
        时返回空元组，账本损坏继续由既有读取路径明确抛错。
        """
        return tuple(
            issue
            for issue in self._ledger_issues()
            if issue.blocking and issue.status is IssueStatus.OPEN
        )

    def _validate_final_ballot(self, ballot: Ballot) -> None:
        """校验终局投票的可选反例合同与 Ledger 阻断条件。

        参数为已完成身份/HEAD/GoalContract 绑定的投票；无返回值；启用反例投票却缺少
        字段、或有未关闭阻断 Issue 仍投 complete 时抛 ``ValueError``。不读取其他 Agent
        的投票，保持 Blind Ballot 隔离。
        """
        if self.config.counterexample_ballot:
            ballot.require_counterexample()
        unresolved = self._unresolved_blocking_ledger_issues()
        if unresolved and ballot.completion is CompletionAssessment.COMPLETE:
            issue_ids = ", ".join(issue.issue_id for issue in unresolved)
            raise ValueError(
                f"存在未关闭阻断 Ledger Issue 时必须投 incomplete: {issue_ids}"
            )

    def _node_collect_blind_ballots(self, state: GraphState) -> Mapping[str, Any]:
        """串行收集盲投票；参数为状态，返回绑定同一 HEAD 的 ballots。"""
        workspace = self._ensure_workspace()
        head = workspace.head()
        for agent in self.config.participants:
            self._sync_agent(agent)

        def collector(agent: AgentName, candidate: GraphState) -> Ballot:
            """调用单个盲投票；参数为 Agent/候选，返回 Ballot，失败时抛错。"""
            return self._call_agent(
                agent=agent,
                response_model=Ballot,
                schema_name="ballot.schema.json",
                prompt=self._ballot_prompt(agent, head),
                phase="ballot",
                turn_id=f"BALLOT-{state.get('current_turn', 0)}",
                read_only=True,
            )

        ballots = collect_blind_ballots(
            state=state,
            current_head=head,
            goal_contract_version=self._contract().version,
            fingerprint=lambda: workspace.snapshot().fingerprint,
            collector=collector,
            ballot_validator=self._validate_final_ballot,
            participants=self.config.participants,
        )
        return {"ballots": ballots}

    def _audit_input(self, state: GraphState) -> AuditBundleInput:
        """从机器记录合成显式审计输入；参数为状态，返回输入，不读取 raw 输出。"""
        self._ensure_components()
        assert self.memory is not None
        turns: list[tuple[TurnResult, str | None]] = []
        for path in sorted(self.artifacts.invocations_dir.glob("*.result.json")):
            result = AgentResult.model_validate_json(path.read_text(encoding="utf-8"))
            if result.structured_output is None:
                continue
            try:
                turn = TurnResult.model_validate(result.structured_output)
            except ValidationError:
                continue
            invocation_id = path.name.removesuffix(".result.json")
            invocation = self.artifacts.read_invocation(invocation_id)
            checkpoint_commit = invocation.get("commit_sha")
            turns.append(
                (
                    turn,
                    checkpoint_commit if isinstance(checkpoint_commit, str) else None,
                )
            )
        summaries = tuple(self.memory.load_change_summaries())
        contributions = {
            agent: tuple(
                turn.summary
                for turn, checkpoint_commit in turns
                if turn.agent is agent
                and checkpoint_commit is not None
                and not turn.turn_id.startswith("REVIEW-")
            )
            for agent in self.config.participants
        }
        decisions = tuple(
            DecisionRecord(
                agent=turn.agent,
                stage="review" if turn.turn_id.startswith("REVIEW-") else "writer",
                decision=turn.decision,
                rationale=turn.summary,
                commit_sha=(
                    turn.observed_head
                    if turn.turn_id.startswith("REVIEW-")
                    else checkpoint_commit or turn.observed_head
                ),
            )
            for turn, checkpoint_commit in turns
        )
        reports = []
        for path in sorted((self.artifacts.run_dir / "validation").glob("*.json")):
            try:
                reports.append(
                    ValidationReport.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                )
            except ValidationError:
                continue
        ballot_payload = state.get("ballots", {})
        ballots = {
            AgentName(agent): Ballot.model_validate(payload)
            for agent, payload in ballot_payload.items()
        }
        process_disputes = tuple(
            f"已解决的过程争议（由最终双 complete 与权威验证关闭）: {issue.claim}"
            for turn, _commit in turns
            for issue in turn.issues
        )
        tree_disputes = tuple(
            f"TreeAck 历史异议 {agent}: {ack.get('rationale', '')}"
            for agent, ack in state.get("tree_acks", {}).items()
            if not ack.get("accepted", False)
        )
        facilitator_records = []
        for path in sorted((self.artifacts.run_dir / "facilitator").glob("*.json")):
            try:
                record = FacilitatorRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except ValidationError:
                continue
            if record.status is not FacilitatorStatus.PREPARED:
                facilitator_records.append(record)
        limitations = (
            (str(state.get("stop_reason")),)
            if state.get("stop_reason")
            else ("无已知未解决限制",)
        )
        behavior = tuple(
            item for summary in summaries for item in summary.behavior_summary
        ) or (self.config.topic,)
        ledger = self._ledger_snapshot()
        agent_runtime_metadata = {}
        for agent in self.config.participants:
            profile = self.config.profiles[agent]
            session = self.sessions.get(agent) if self.sessions is not None else None
            session_ref = (
                "sha256:" + hashlib.sha256(session.session_id.encode()).hexdigest()
                if session is not None and session.session_id
                else None
            )
            agent_runtime_metadata[agent] = AgentAuditMetadata(
                version=profile.version,
                command=Path(profile.command).name,
                model=profile.model,
                variant=profile.variant,
                native_profile=profile.native_profile,
                provider=profile.provider,
                experimental=agent_definition(agent).experimental,
                prompt_transport=(
                    PromptTransport.TEMP_FILE.value
                    if agent is AgentName.OPENCLAW
                    else PromptTransport.ARGV.value
                    if agent is AgentName.HERMES
                    else PromptTransport.STDIN.value
                ),
                session_generation=session.generation if session else None,
                preflight_status="passed",
                session_ref=session_ref,
                runtime_config_hash=profile.runtime_config_hash,
                runtime_config_content_hash=profile.runtime_config_content_hash,
                runtime_config_resolved_hash=profile.runtime_config_resolved_hash,
            )
        return AuditBundleInput(
            goal_contract=self._contract(),
            participants=self.config.participants,
            behavior_changes=behavior,
            contributions=contributions,
            change_summaries=summaries,
            decisions=decisions,
            disputes=process_disputes + tree_disputes or ("无未解决争议",),
            limitations=limitations,
            validation_reports=tuple(reports),
            ballots=ballots,
            facilitator_records=tuple(facilitator_records),
            ledger_protocol_version=(
                ledger.protocol_version if ledger is not None else None
            ),
            ledger_issues=ledger.issues if ledger is not None else (),
            ledger_transitions=ledger.transitions if ledger is not None else (),
            agent_runtime_metadata=agent_runtime_metadata,
        )

    def _node_generate_audit_bundle(self, state: GraphState) -> Mapping[str, Any]:
        """生成完成审计包；参数为状态，返回审计路径。"""
        writer = AuditBundleWriter(
            worktree=self._ensure_workspace().worktree_path, run_id=self.run_id
        )
        writer.write_completed(self._audit_input(state))
        return {"audit_path": writer.relative_path}

    def _node_materialize_final_tree(self, state: GraphState) -> Mapping[str, Any]:
        """暂存审计并物化 tree；参数为状态，返回 final candidate。"""
        coordinator = FinalTreeCoordinator(self._ensure_workspace())
        candidate = coordinator.materialize_final_tree(
            str(
                state.get(
                    "audit_path",
                    select_audit_relative_path(
                        self._ensure_workspace().worktree_path, self.run_id
                    ),
                )
            )
        )
        if (
            state.get("audit_dispute", False)
            and state.get("final_tree_sha") == candidate.final_tree_sha
        ):
            raise FinalTreeError("审计异议重生成后 tree 未变化，无法机械消解")
        return {
            "final_tree_sha": candidate.final_tree_sha,
            "final_candidate": asdict(candidate),
        }

    def _node_collect_tree_acks(self, state: GraphState) -> Mapping[str, Any]:
        """收集参与者 final tree ack；参数为状态，返回 Ack 与 dispute 标记。"""
        candidate = FinalTreeCandidate(**state["final_candidate"])
        coordinator = FinalTreeCoordinator(self._ensure_workspace())

        def collector(agent: AgentName, tree: FinalTreeCandidate) -> TreeAck:
            """调用单个 tree ack；参数为 Agent/候选，返回 TreeAck。"""
            prompt = (
                f"你是 {agent.value}。严格只读检查 staged final tree 与仓库内审计。\n"
                f"FINAL_TREE_SHA={tree.final_tree_sha}\nAUDIT_PATH={tree.audit_path}\n"
                "确认产品代码未漂移、审计准确且未泄露敏感信息。审计中的"
                " ready-for-final-tree-ack 是固定候选的正确状态，不是未完成缺陷；"
                "本次全员 Ack 只会在成功后的最终提交 Final-Tree-Ack trailer 中记录，"
                "不会写回审计文件，以免形成自引用并改变正在确认的 tree。"
                "不要要求审计包含当前 tree SHA、此次 Ack 或先前 TreeAck 异议。"
                "返回 TreeAck JSON。"
            )
            return self._call_agent(
                agent=agent,
                response_model=TreeAck,
                schema_name="tree-ack.schema.json",
                prompt=prompt,
                phase="tree-ack",
                turn_id=f"TREE-{tree.final_tree_sha[:12]}",
                read_only=True,
            )

        acks = coordinator.collect_tree_acks(
            candidate,
            collector=collector,
            participants=self.config.participants,
        )
        return {
            "tree_acks": {
                agent.value: ack.model_dump(mode="json") for agent, ack in acks.items()
            },
            "tree_acks_complete": all(ack.accepted for ack in acks.values()),
            "audit_dispute": any(not ack.accepted for ack in acks.values()),
        }

    def _node_create_integration_commit(self, state: GraphState) -> Mapping[str, Any]:
        """创建聚焦提交；参数为状态，返回 SHA，门禁不满足时抛错。"""
        candidate = FinalTreeCandidate(**state["final_candidate"])
        acks = {
            AgentName(agent): TreeAck.model_validate(payload)
            for agent, payload in state["tree_acks"].items()
        }
        ballots = {
            AgentName(agent): Ballot.model_validate(payload)
            for agent, payload in state["ballots"].items()
        }
        commit = FinalTreeCoordinator(
            self._ensure_workspace()
        ).create_integration_commit(
            candidate,
            tree_acks=acks,
            ballots=ballots,
            subject="feat: deliver debated agent result",
            goal_contract_version=self._contract().version,
            validation_status="passed",
            participants=self.config.participants,
        )
        return {"integration_commit": commit}

    def _node_integrate_target(self, state: GraphState) -> Mapping[str, Any]:
        """安全快进目标分支；参数为状态，返回成功标记，漂移时抛错。"""
        commit, tree = self._ensure_workspace().integrate_target(
            state["integration_commit"], state["final_tree_sha"]
        )
        return {
            "integration_commit": commit,
            "integrated_tree_sha": tree,
            "integration_succeeded": True,
        }

    def _node_preserve_result(self, state: GraphState) -> Mapping[str, Any]:
        """保留最终结果分支；参数为状态，返回结果位置，Git 门禁失败时抛错。"""
        workspace = self._ensure_workspace()
        workspace.preserve_result(state["integration_commit"], state["final_tree_sha"])
        return {
            "current_head": state["integration_commit"],
            "integration_skipped": True,
            "result_branch": workspace.debate_branch,
            "result_worktree": str(workspace.worktree_path),
        }

    def _node_verify_integration(self, state: GraphState) -> Mapping[str, Any]:
        """验证目标 tree；参数为状态，返回空更新，不一致时抛集成错误。"""
        workspace = self._ensure_workspace()
        actual = workspace.target_tree()
        expected = state.get("integrated_tree_sha", state["final_tree_sha"])
        if actual != expected:
            raise IntegrationBlockedError("目标分支 tree 与 final tree 不一致")
        return {}

    def _node_cleanup_success(self, state: GraphState) -> Mapping[str, Any]:
        """清理成功资源；参数为状态，返回 completed，清理失败时抛错。"""
        if not state.get("integration_skipped", False):
            self._ensure_workspace().cleanup_success()
        self.artifacts.cleanup_sensitive()
        self.artifacts.write_status(RunStatus.COMPLETED, stop_reason=None)
        return {"status": RunStatus.COMPLETED.value, "stop_reason": None}
