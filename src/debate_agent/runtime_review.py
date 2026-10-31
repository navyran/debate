"""Debate Runtime 的结构化审查、Issue Ledger 和 Facilitator 阶段。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from debate_agent.facilitator import compile_facilitator_hint
from debate_agent.git_workspace import (
    WorkspaceRaceError,
)
from debate_agent.graph import (
    GraphState,
    apply_facilitator_writer_hint,
    decide_progress,
    record_semantic_progress,
)
from debate_agent.issue_ledger import (
    IssueLedger,
    IssueLedgerStore,
    LedgerIssue,
)
from debate_agent.memory import MemoryStore
from debate_agent.models import (
    AgentName,
    ChangeSummary,
    CompletionAssessment,
    FacilitatorContext,
    FacilitatorPurpose,
    FacilitatorTurnSummary,
    LeaseState,
    ReviewProtocolVersion,
    RunStatus,
    TreeAck,
    TurnResult,
)
from debate_agent.prompts import (
    build_reviewer_prompt,
)
from debate_agent.review_models import ReviewCoverageStatus, ReviewResult
from debate_agent.review_policy import EvidenceResolver, ReviewPolicy
from debate_agent.review_runtime import ReviewInvocation, ReviewRuntime
from debate_agent.runtime_debate import DebatePhaseRuntime
from debate_agent.validation import (
    ValidationPhase,
    ValidationReport,
)


class ReviewPhaseRuntime(DebatePhaseRuntime):
    """实现当前 HEAD 审查、Ledger 和语义路由。"""

    def _review_head_validation(self, *, report_id: str) -> ValidationReport:
        """在 Writer checkpoint 提交后重新验证当前 HEAD。

        参数为图状态和安全报告标识；返回由 Supervisor 生成并绑定当前 HEAD 的权威
        报告。验证命令修改工作树、报告未绑定当前提交或执行失败时抛异常。该报告是
        review-v1 的唯一 validation_report/test_result 证据来源，绝不复用提交前报告。
        """
        self._ensure_components()
        assert self.validation_runner is not None
        workspace = self._ensure_workspace()
        head = workspace.head()
        before = workspace.snapshot().fingerprint
        report = self.validation_runner.run(
            report_id=report_id,
            phase=ValidationPhase.CHECKPOINT,
            commands=self._contract().all_validation_commands,
            cwd=workspace.worktree_path,
            env=self.env,
            head_sha=head,
        )
        if workspace.snapshot().fingerprint != before:
            raise WorkspaceRaceError("提交后的权威验证命令修改了受控工作树")
        if report.head_sha != head:
            raise RuntimeError("提交后的验证报告未绑定当前 HEAD")
        return report

    def _node_publish_handoff(self, state: GraphState) -> Mapping[str, Any]:
        """生成 ChangeSummary/PeerMemoryUpdate；参数为状态，返回引用和 pending。"""
        self._ensure_components()
        assert self.memory is not None
        if not state.get("made_checkpoint", False):
            return {}
        invocation_id = state.get("current_invocation_id")
        if invocation_id:
            self.artifacts.mark_invocation_checkpointed(invocation_id)
        turn = TurnResult.model_validate(state["turn_result"])
        lease = LeaseState.model_validate(state["lease"])
        ledger = self._ledger_snapshot()
        if ledger is None:
            opened_issues = None
            resolved_issues: list[str] = []
            requested_attention = [issue.claim for issue in turn.issues]
        else:
            issues = ledger.issues
            opened_issues = [
                issue.issue_id
                for issue in issues
                if issue.status.value == "open" and issue.blocking
            ]
            resolved_issues, requested_attention = (
                self.memory.project_ledger_for_peer_memory(issues)
            )
        summary = self.memory.build_change_summary(
            result=turn,
            parent_commit=lease.base_commit,
            commit_sha=state["last_commit"],
            changed_paths=state.get("validated_paths", []),
            opened_issues=opened_issues,
            resolved_issues=resolved_issues,
            validation_status=(
                "passed"
                if state.get("checkpoint_validation_passed", False)
                else "failed-validation"
            ),
            validation_report_ref=state["validation_report_ref"],
            requested_attention=requested_attention,
        )
        ref = self.memory.save_change_summary(summary)
        for target in self.config.participants:
            if target is not turn.agent:
                self.memory.publish(summary, target_agent=target)
        return {
            "latest_change_summary_ref": ref,
            "pending_update_refs": {
                agent.value: self.memory.pending(agent)
                for agent in self.config.participants
            },
        }

    def _review_prompt(self, *, reviewer: AgentName, turn_id: str) -> str:
        """生成只读 Reviewer Prompt；参数为 Agent/turn，返回审查文本。"""
        head = self._ensure_workspace().head()
        return build_reviewer_prompt(
            reviewer=reviewer.value,
            head_sha=head,
            goal_contract_json=self._contract().model_dump_json(indent=2),
            turn_id=turn_id,
        )

    def _ledger_store(self) -> IssueLedgerStore | None:
        """按冻结配置获取可选 Ledger Store。

        无参数；当且仅当 review-v1 且显式启用时返回 Store；旧协议和默认关闭 Run
        返回 ``None``，Ledger 损坏时由 Store 明确抛错而不静默降级。
        """
        if (
            self.config.protocol_version is not ReviewProtocolVersion.REVIEW_V1
            or not self.config.issue_ledger_enabled
        ):
            return None
        return IssueLedgerStore(artifacts=self.artifacts)

    def _ledger_issues(self) -> tuple[LedgerIssue, ...]:
        """读取当前 Ledger 的唯一 Issue 投影；无参数，返回元组，损坏时抛异常。"""
        ledger = self._ledger_snapshot()
        return ledger.issues if ledger is not None else ()

    def _ledger_snapshot(self) -> IssueLedger | None:
        """读取当前唯一 Ledger 快照。

        无参数；返回启用账本时的持久化快照或 ``None``；损坏账本由 Store 明确停止
        恢复，绝不以空视图掩盖。该快照是 Audit/Peer Memory/Facilitator 的共同来源，
        legacy-v0 和默认关闭场景不读取或伪造 Ledger。
        """
        store = self._ledger_store()
        return store.load() if store is not None else None

    def _ledger_attention(self) -> list[str]:
        """派生注入 CLI 的未关闭阻断提示；无参数，返回摘要，不写入独立状态。"""
        _resolved, attention = MemoryStore.project_ledger_for_peer_memory(
            self._ledger_issues()
        )
        return attention

    @staticmethod
    def _ledger_review_context(
        issues: tuple[LedgerIssue, ...],
    ) -> tuple[dict[str, str], ...]:
        """投影本轮 Reviewer 必须处置的既有 OPEN Issue。

        参数为唯一 Ledger 的 Issue 投影；返回仅含稳定 ID、生命周期和必要上下文的
        有界字典元组。不按 claim 匹配、不创建新状态；Reviewer 的输出由后续策略层
        校验。
        """
        return tuple(
            {
                "issue_id": issue.issue_id,
                "dimension": issue.dimension.value,
                "claim": issue.claim[:1000],
                "opened_head": issue.opened_head,
                "opened_by": issue.opened_by.value,
                "addressed_by_commit": issue.addressed_by_commit or "",
                "addressed_by_agent": (
                    issue.addressed_by_agent.value
                    if issue.addressed_by_agent is not None
                    else ""
                ),
            }
            for issue in issues
            if issue.status.value == "open" and issue.blocking
        )

    def _record_writer_ledger_addresses(
        self,
        *,
        writer: AgentName,
        commit_sha: str,
        changed_paths: tuple[str, ...],
        checkpoint_validation_passed: bool,
    ) -> None:
        """按当前 checkpoint 的真实变更登记 Writer 对既有 Issue 的处理。

        参数为 Writer、当前提交、已由写策略验证的改动路径和验证结果；无返回值。路径
        相交，或带验证/测试证据的 Issue 在通过的检查点上有真实改动时，才登记
        ``addressed_by_commit``；不依据 Writer 自述关闭或猜测 Issue 已修复，仍需
        独立 Reviewer 的当前 HEAD 证据；当前 HEAD 不一致或账本失败时抛错。
        """
        store = self._ledger_store()
        if store is None:
            return
        current_head = self._ensure_workspace().head()
        if commit_sha != current_head:
            raise RuntimeError("Writer Ledger 登记未绑定当前 HEAD")
        changed = set(changed_paths)
        for issue in store.blocking_projection():
            has_runtime_evidence = any(
                reference.kind.value in {"validation_report", "test_result"}
                for reference in issue.evidence_refs
            )
            addresses_path = bool(changed.intersection(issue.affected_paths))
            addresses_runtime_evidence = bool(
                changed and checkpoint_validation_passed and has_runtime_evidence
            )
            if addresses_path or addresses_runtime_evidence:
                store.record_addressed(
                    issue_id=issue.issue_id,
                    writer=writer,
                    commit_sha=commit_sha,
                    current_head=current_head,
                )

    def _publish_ledger_handoff(
        self,
        *,
        review: ReviewResult,
        validation_report_ref: str,
        validation_passed: bool,
    ) -> tuple[str, dict[str, list[str]]]:
        """在 Reviewer 更新 Ledger 后发布同一事实来源的 Peer Memory。

        参数为已持久化并校验的 Review、当前 HEAD 验证报告及其结果；返回摘要引用
        和所有 Agent 的待同步游标。只从 Ledger 投影已关闭与未决 Issue，防止下一
        Writer 读取到审查前的旧交接；发布失败时抛出底层记忆协议错误。
        """
        self._ensure_components()
        assert self.memory is not None
        issues = self._ledger_issues()
        resolved_issues, requested_attention = (
            self.memory.project_ledger_for_peer_memory(issues)
        )
        opened_issues = [
            issue.issue_id
            for issue in issues
            if issue.status.value == "open" and issue.blocking
        ]
        summary = ChangeSummary(
            update_id=f"UPD-{review.turn_id}-{review.agent.value}-ledger",
            source_agent=review.agent,
            turn_id=review.turn_id,
            parent_commit=review.head_sha,
            commit_sha=review.head_sha,
            goal_contract_version=review.goal_contract_version,
            changed_paths=[],
            behavior_summary=[review.summary],
            opened_issues=opened_issues,
            resolved_issues=resolved_issues,
            validation_status="passed" if validation_passed else "failed",
            validation_report_ref=validation_report_ref,
            requested_attention=requested_attention,
        )
        reference = self.memory.save_change_summary(summary)
        for agent in self.config.participants:
            if agent is not review.agent:
                self.memory.publish(summary, target_agent=agent)
        return (
            reference,
            {
                agent.value: self.memory.pending(agent)
                for agent in self.config.participants
            },
        )

    def _node_review_current_head(self, state: GraphState) -> Mapping[str, Any]:
        """同步并调用下一 Reviewer；参数为状态，返回参与者完成判断。"""
        writer = TurnResult.model_validate(state["turn_result"])
        reviewer = self._next_participant(writer.agent)
        self._sync_agent(reviewer)
        head = self._ensure_workspace().head()
        review_turn = f"REVIEW-{writer.turn_id}"
        if self.config.protocol_version is ReviewProtocolVersion.REVIEW_V1:
            report = self._review_head_validation(
                report_id=f"REVIEW-HEAD-{writer.turn_id}-{head[:12]}",
            )
            ledger_store = self._ledger_store()
            open_issues = (
                ledger_store.blocking_projection() if ledger_store is not None else ()
            )
            review_policy = ReviewPolicy(
                resolver=EvidenceResolver(
                    workspace=self._ensure_workspace(), artifacts=self.artifacts
                )
            )
            persisted_review = self.artifacts.find_review_result(
                head_sha=head,
                reviewer=reviewer,
                turn_id=review_turn,
            )
            if persisted_review is None:
                review_runtime = ReviewRuntime(
                    invoke=lambda prompt: self._call_agent(
                        agent=reviewer,
                        response_model=ReviewResult,
                        schema_name="review.schema.json",
                        prompt=prompt,
                        phase="review-v1",
                        turn_id=review_turn,
                        read_only=True,
                    ),
                    fingerprint=lambda: self._ensure_workspace().snapshot().fingerprint,
                    policy=review_policy,
                    persist=self.artifacts.write_review_result,
                )
                review_result, review_ref = review_runtime.run(
                    ReviewInvocation(
                        reviewer=reviewer,
                        turn_id=review_turn,
                        head_sha=head,
                        goal_contract_version=self._contract().version,
                        goal_contract_json=self._contract().model_dump_json(indent=2),
                        validation_report_ref=report.report_ref,
                        open_issues=self._ledger_review_context(open_issues),
                    )
                )
            else:
                review_result = persisted_review
                review_ref = f"reviews/{head}.{reviewer.value}.{review_turn}.json"
                review_policy.validate_review(review_result)
            ledger_handoff: tuple[str, dict[str, list[str]]] | None = None
            if ledger_store is not None:
                validated_evidence_refs = tuple(
                    item.evidence
                    for item in review_policy.validate_review(review_result)
                )
                ledger_store.apply_review_dispositions(
                    review=review_result,
                    current_head=head,
                    validated_evidence_refs=validated_evidence_refs,
                )
                ledger_store.register_review(review_result, current_head=head)
                ledger_handoff = self._publish_ledger_handoff(
                    review=review_result,
                    validation_report_ref=report.report_ref,
                    validation_passed=report.passed,
                )
            blocking = [
                f"{finding.finding_id}: {finding.claim}"[:1000]
                for finding in review_result.findings
                if finding.blocking
            ]
            blocking.extend(
                f"{coverage.dimension.value}: {coverage.summary}"[:1000]
                for coverage in review_result.coverage
                if coverage.status is ReviewCoverageStatus.UNKNOWN
            )
            blocking.extend(self._ledger_attention())
            assessments = {
                **state.get("assessments", {}),
                writer.agent.value: writer.completion_assessment.value,
                reviewer.value: review_result.completion_assessment.value,
            }
            semantic_progress = record_semantic_progress(
                state,
                assessments=assessments,
                review_blocking_summary=blocking,
                scope_requests=state.get("scope_requests", {}),
                checkpoint_validation_passed=report.passed,
            )
            result: dict[str, Any] = {
                "review_result": review_result.model_dump(mode="json"),
                "review_result_ref": review_ref,
                "review_blocking_summary": blocking,
                "review_protocol_version": self.config.protocol_version.value,
                "validation_report_ref": report.report_ref,
                "checkpoint_validation_passed": report.passed,
                "assessments": assessments,
                **semantic_progress,
            }
            if ledger_handoff is not None:
                result["latest_change_summary_ref"] = ledger_handoff[0]
                result["pending_update_refs"] = ledger_handoff[1]
            return result
        review = self._call_agent(
            agent=reviewer,
            response_model=TurnResult,
            schema_name="turn.schema.json",
            prompt=self._review_prompt(reviewer=reviewer, turn_id=review_turn),
            phase="review",
            turn_id=review_turn,
            read_only=True,
        )
        if review.base_commit != head or review.observed_head != head:
            raise RuntimeError("Reviewer TurnResult 未绑定当前 HEAD")
        scope_requests = dict(state.get("scope_requests", {}))
        scope_requests[writer.agent.value] = writer.requested_scope_additions
        scope_requests[reviewer.value] = review.requested_scope_additions
        assessments = {
            **state.get("assessments", {}),
            writer.agent.value: writer.completion_assessment.value,
            reviewer.value: review.completion_assessment.value,
        }
        semantic_progress = record_semantic_progress(
            state,
            assessments=assessments,
            review_blocking_summary=[],
            scope_requests=scope_requests,
            checkpoint_validation_passed=bool(
                state.get("checkpoint_validation_passed", False)
            ),
        )
        return {
            "review_result": review.model_dump(mode="json"),
            "assessments": assessments,
            **semantic_progress,
            "scope_requests": scope_requests,
            "review_protocol_version": self.config.protocol_version.value,
            "review_blocking_summary": [],
        }

    def _node_update_goal_contract(self, state: GraphState) -> Mapping[str, Any]:
        """处理双方结构化增量请求；参数为状态，返回契约版本，越权时抛错。"""
        writer = TurnResult.model_validate(state["turn_result"])
        if self.config.protocol_version is ReviewProtocolVersion.REVIEW_V1:
            return {"goal_contract_version": self._contract().version}
        reviewer = TurnResult.model_validate(state["review_result"])
        requests = state.get("scope_requests", {})
        participant_requests = [
            set(requests[agent.value])
            for agent in self.config.participants
            if agent.value in requests
        ]
        shared_scope = (
            sorted(set.intersection(*participant_requests))
            if len(participant_requests) == len(self.config.participants)
            else []
        )
        validations = tuple(
            tuple(command)
            for command in (
                *writer.requested_validation_additions,
                *reviewer.requested_validation_additions,
            )
        )
        if not shared_scope and not validations:
            return {"goal_contract_version": self._contract().version}
        evolved = self.contract_store.evolve(
            self._contract(),
            acceptance_additions=(),
            validation_additions=validations,
            derived_scope_additions=shared_scope,
            scope_approvals=set(self.config.participants) if shared_scope else set(),
            command_policy=self.command_policy,
            required_agents=set(self.config.participants),
        )
        for agent in self.config.participants:
            self._sync_agent(agent)
            self._confirm_contract(agent, version=evolved.version)
        self.contract_store.assert_both_confirmed(
            version=evolved.version,
            participants=self.config.participants,
        )
        return {
            "goal_contract_version": evolved.version,
            "goal_contract_ref": f"goal-contract/v{evolved.version}.json",
            "scope_requests": {} if shared_scope else requests,
        }

    @staticmethod
    def _facilitator_turn_summary(
        turn: TurnResult | ReviewResult,
    ) -> FacilitatorTurnSummary:
        """压缩 Writer 或结构化 Reviewer 结果。

        参数为 TurnResult 或 ReviewResult；返回脱敏 Facilitator 摘要；模型内容非法时由
        上游模型校验阻止。本方法仅投影，不生成 Evidence 或业务结论。
        """
        if isinstance(turn, ReviewResult):
            return FacilitatorTurnSummary(
                agent=turn.agent,
                decision=turn.decision,
                summary=turn.summary[:4000],
                issues=[
                    f"{finding.finding_id}: {finding.claim}"[:1000]
                    for finding in turn.findings
                    if finding.blocking
                ],
                completion_assessment=turn.completion_assessment,
            )
        return FacilitatorTurnSummary(
            agent=turn.agent,
            decision=turn.decision,
            summary=turn.summary[:4000],
            issues=[f"{issue.id}: {issue.claim}"[:1000] for issue in turn.issues],
            completion_assessment=turn.completion_assessment,
        )

    @staticmethod
    def _review_evidence_projection(review: ReviewResult) -> list[str]:
        """投影已通过机器校验的 Reviewer 证据引用。

        参数为已由 ReviewPolicy 校验的 ReviewResult；返回去重后的相对 Git/产物引用。
        空引用不进入投影；本方法不创建、变更或重新解释任何 EvidenceRef。
        """
        references = [
            reference.artifact_ref or reference.path
            for coverage in review.coverage
            for reference in coverage.evidence_refs
        ]
        references.extend(
            reference.artifact_ref or reference.path
            for finding in review.findings
            for reference in finding.evidence_refs
        )
        return list(dict.fromkeys(reference for reference in references if reference))

    def _node_facilitator_assess(self, state: GraphState) -> Mapping[str, Any]:
        """执行回合语义建议；参数为状态，返回安全 hint 与记录引用。"""
        self._ensure_components()
        assert self.memory is not None
        writer = TurnResult.model_validate(state["turn_result"])
        reviewer: TurnResult | ReviewResult
        unresolved_review_criteria: list[str] = []
        review_evidence_refs: list[str] = []
        if self.config.protocol_version is ReviewProtocolVersion.REVIEW_V1:
            reviewer = ReviewResult.model_validate(state["review_result"])
            unresolved_review_criteria = list(state.get("review_blocking_summary", []))
            review_evidence_refs = self._review_evidence_projection(reviewer)
            ledger_attention = self._ledger_attention()
            if ledger_attention:
                unresolved_review_criteria.extend(ledger_attention)
        else:
            reviewer = TurnResult.model_validate(state["review_result"])
        summaries = self.memory.load_change_summaries()
        latest = summaries[-1] if summaries else None
        contract = self._contract()
        assessments = {
            AgentName(agent): CompletionAssessment(value)
            for agent, value in state.get("assessments", {}).items()
        }
        context = FacilitatorContext(
            run_id=self.run_id,
            purpose=FacilitatorPurpose.TURN_ROUTING,
            turn_id_or_revision=writer.turn_id,
            head_sha=self._ensure_workspace().head(),
            goal_contract_version=contract.version,
            objective=contract.objective,
            deliverables=list(contract.deliverables),
            acceptance_criteria=list(contract.acceptance_criteria),
            effective_scope=list(contract.effective_scope),
            writer=self._facilitator_turn_summary(writer),
            reviewer=self._facilitator_turn_summary(reviewer),
            changed_paths=list(latest.changed_paths) if latest else [],
            behavior_summary=list(latest.behavior_summary) if latest else [],
            validation_passed=bool(state.get("checkpoint_validation_passed", False)),
            validation_report_ref=state.get("validation_report_ref"),
            unresolved_review_criteria=unresolved_review_criteria,
            review_evidence_refs=review_evidence_refs,
            assessments=assessments,
            stall_count=state.get("stall_count", 0),
            remaining_turns=max(
                state.get("max_turns", 0) - state.get("current_turn", 0), 0
            ),
            previous_decision_ref=state.get("facilitator_decision_ref"),
        )
        record = self.facilitator.evaluate(context)
        next_writer, instruction = compile_facilitator_hint(
            record, assessments=assessments
        )
        tentative = {
            **state,
            "facilitator_next_writer": next_writer.value if next_writer else None,
        }
        progress = apply_facilitator_writer_hint(
            cast(GraphState, tentative), decide_progress(state)
        )
        route = str(progress.get("gate", "stop"))
        if progress.get("next_writer"):
            route = f"{route}:{progress['next_writer']}"
        record = record.model_copy(update={"deterministic_route": route})
        reference = self.artifacts.write_facilitator_record(
            record.decision_id, record.model_dump(mode="json")
        )
        return {
            "facilitator_decision_ref": reference,
            "facilitator_instruction_ref": reference if instruction else None,
            "facilitator_status": record.status.value,
            "facilitator_mode": record.mode.value,
            "facilitator_next_writer": next_writer.value if next_writer else None,
        }

    def _node_facilitator_audit_dispute(self, state: GraphState) -> Mapping[str, Any]:
        """分析审计 TreeAck 异议；参数为状态，返回审计建议引用。"""
        if not state.get("audit_dispute", False):
            return {
                "status": RunStatus.FAILED.value,
                "stop_reason": "没有审计异议却进入 Facilitator 节点",
            }
        contract = self._contract()
        acks = {
            AgentName(agent): TreeAck.model_validate(payload)
            for agent, payload in state.get("tree_acks", {}).items()
        }
        disputes = [
            f"{agent.value}: {ack.rationale}"[:2000]
            for agent, ack in acks.items()
            if not ack.accepted
        ]
        assessments = {
            AgentName(agent): CompletionAssessment(value)
            for agent, value in state.get("assessments", {}).items()
        }
        context = FacilitatorContext(
            run_id=self.run_id,
            purpose=FacilitatorPurpose.AUDIT_DISPUTE,
            turn_id_or_revision=(
                f"AUDIT-{str(state.get('final_tree_sha', 'none'))[:12]}"
            ),
            head_sha=self._ensure_workspace().head(),
            goal_contract_version=contract.version,
            objective=contract.objective,
            deliverables=list(contract.deliverables),
            acceptance_criteria=list(contract.acceptance_criteria),
            effective_scope=list(contract.effective_scope),
            changed_paths=[],
            behavior_summary=[],
            validation_passed=bool(state.get("final_validation_passed", False)),
            validation_report_ref=state.get("validation_report_ref"),
            assessments=assessments,
            stall_count=state.get("stall_count", 0),
            remaining_turns=max(
                state.get("max_turns", 0) - state.get("current_turn", 0), 0
            ),
            audit_disputes=disputes,
            previous_decision_ref=state.get("facilitator_decision_ref"),
        )
        record = self.facilitator.evaluate(context).model_copy(
            update={"deterministic_route": "regenerate_audit"}
        )
        reference = self.artifacts.write_facilitator_record(
            record.decision_id, record.model_dump(mode="json")
        )
        return {
            "facilitator_decision_ref": reference,
            "facilitator_instruction_ref": None,
            "facilitator_status": record.status.value,
            "facilitator_mode": record.mode.value,
            "facilitator_next_writer": None,
        }

    def _node_progress_gate(self, state: GraphState) -> Mapping[str, Any]:
        """补充总时限/abort 门禁；参数为状态，返回可选终止更新。"""
        if self.artifacts.abort_requested():
            return {
                "status": RunStatus.ABORTED.value,
                "stop_reason": "收到 abort 请求",
            }
        elapsed = self._active_elapsed_seconds(state)
        updates: dict[str, Any] = {"active_elapsed_seconds": elapsed}
        if elapsed >= self.config.total_timeout_seconds:
            updates.update(
                {
                    "status": RunStatus.FAILED.value,
                    "stop_reason": "达到累计活跃运行时限",
                }
            )
        return updates
