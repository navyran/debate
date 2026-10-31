from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from debate_agent.graph import (
    REQUIRED_NODE_NAMES,
    BlindBallotError,
    GraphState,
    SQLiteCheckpoint,
    apply_facilitator_writer_hint,
    build_debate_graph,
    collect_blind_ballots,
    decide_progress,
    invoke_debate_graph,
    record_semantic_progress,
    resume_debate_graph,
)
from debate_agent.models import (
    AgentName,
    Ballot,
    CompletionAssessment,
    ReviewProtocolVersion,
    RunStatus,
)


class ScriptedRuntime:
    """为图测试提供确定性节点结果。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.review_count = 0
        self.validation_results = [False, True]

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        self.calls.append(name)
        if name == "ensure_sessions":
            return {"session_generations": {"codex": 2, "claude": 1}}
        if name == "run_checkpoint_validation":
            return {"checkpoint_validation_passed": self.validation_results.pop(0)}
        if name == "review_current_head":
            self.review_count += 1
            completion = "complete" if self.review_count >= 2 else "incomplete"
            return {
                "assessments": {"codex": completion, "claude": completion},
                "stall_count": 0,
            }
        if name == "run_final_validation":
            return {"final_validation_passed": True}
        if name == "collect_blind_ballots":
            return {
                "ballots": {
                    "codex": {"completion": "complete", "commit_sha": "head"},
                    "claude": {"completion": "complete", "commit_sha": "head"},
                }
            }
        if name == "collect_tree_acks":
            return {"tree_acks_complete": True, "audit_dispute": False}
        if name == "integrate_target":
            return {"integration_succeeded": True}
        if name == "cleanup_success":
            return {"status": RunStatus.COMPLETED.value, "stop_reason": None}
        return {}


def initial_state() -> GraphState:
    return {
        "run_id": "RUN-1",
        "status": RunStatus.RUNNING.value,
        "stop_reason": None,
        "current_turn": 0,
        "max_turns": 4,
        "failure_count": 0,
        "max_process_failures": 2,
        "stall_count": 0,
        "stall_turns": 2,
        "next_writer": "codex",
        "assessments": {"codex": "incomplete", "claude": "incomplete"},
        "resume_requested": False,
        "current_head": "head",
    }


def test_three_agent_progress_prioritizes_incomplete_selected_participant() -> None:
    state: GraphState = {
        **initial_state(),
        "participants": ["codex", "claude", "qoder"],
        "current_writer": "claude",
        "assessments": {
            "codex": "complete",
            "claude": "complete",
            "qoder": "incomplete",
        },
    }

    progress = decide_progress(state)

    assert progress == {"gate": "continue", "next_writer": "qoder"}
    assert apply_facilitator_writer_hint(
        {**state, "facilitator_next_writer": "qoder"}, progress
    )["next_writer"] == "qoder"

    two_incomplete: GraphState = {
        **state,
        "current_writer": "codex",
        "assessments": {
            "codex": "incomplete",
            "claude": "complete",
            "qoder": "incomplete",
        },
    }
    assert decide_progress(two_incomplete)["next_writer"] == "qoder"


def test_review_v1_blocking_summary_prevents_final_but_respects_turn_budget() -> None:
    """验证 review-v1 未解决阻断项不能进入 final，且不会绕过最大回合门禁。"""
    state: GraphState = {
        **initial_state(),
        "review_protocol_version": ReviewProtocolVersion.REVIEW_V1.value,
        "review_blocking_summary": ["FIND-1: 缺少验证"],
        "assessments": {"codex": "complete", "claude": "incomplete"},
    }

    assert decide_progress(state) == {"gate": "continue", "next_writer": "claude"}
    exhausted = {**state, "current_turn": state["max_turns"]}
    assert decide_progress(exhausted)["gate"] == "stop"
    assert decide_progress(exhausted)["status"] == RunStatus.MAX_TURNS.value


def test_graph_contains_all_design_nodes_and_failed_checkpoint_can_be_fixed(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime()
    with SQLiteCheckpoint.open(tmp_path / "checkpoint.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        assert REQUIRED_NODE_NAMES.issubset(graph.nodes)

        result = invoke_debate_graph(graph, initial_state(), run_id="RUN-1")

        assert result["status"] == RunStatus.COMPLETED.value
        assert runtime.calls.count("writer_turn") == 2
        assert runtime.calls.count("commit_checkpoint") == 2
        assert runtime.calls.count("facilitator_assess") == 2
        first_contract = runtime.calls.index("update_goal_contract")
        assert runtime.calls[first_contract + 1] == "facilitator_assess"
        assert runtime.calls[first_contract + 2] == "progress_gate"
        assert result["session_generations"]["codex"] == 2
        snapshot = graph.get_state({"configurable": {"thread_id": "RUN-1"}})
        assert snapshot.values["status"] == RunStatus.COMPLETED.value


def test_graph_preserves_result_when_target_integration_is_disabled(
    tmp_path: Path,
) -> None:
    """验证冻结配置关闭集成时走保留分支；参数为临时目录，无返回值。"""
    runtime = ScriptedRuntime()
    state = initial_state()
    state["integrate_to_target"] = False
    with SQLiteCheckpoint.open(tmp_path / "preserve.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        result = invoke_debate_graph(graph, state, run_id="RUN-PRESERVE")

    assert result["status"] == RunStatus.COMPLETED.value
    assert "preserve_result" in runtime.calls
    assert "integrate_target" not in runtime.calls


def test_progress_gate_requires_both_complete_and_enforces_budgets() -> None:
    state = initial_state()
    state["assessments"] = {"codex": "complete", "claude": "incomplete"}
    assert decide_progress(state)["gate"] == "continue"

    state["assessments"] = {"codex": "complete", "claude": "complete"}
    assert decide_progress(state)["gate"] == "final"

    state["current_turn"] = state["max_turns"]
    state["stall_count"] = state["stall_turns"]
    assert decide_progress(state)["gate"] == "final"

    state["current_turn"] = state["max_turns"]
    state["assessments"] = {"codex": "complete", "claude": "incomplete"}
    stopped = decide_progress(state)
    assert stopped["status"] == RunStatus.MAX_TURNS.value
    assert stopped["stop_reason"]

    state = initial_state()
    state["stall_count"] = state["stall_turns"]
    assert decide_progress(state)["status"] == RunStatus.STALLED.value

    state = initial_state()
    state["failure_count"] = state["max_process_failures"]
    assert decide_progress(state)["status"] == RunStatus.FAILED.value


def test_semantic_progress_resets_stall_without_git_checkpoint() -> None:
    """验证完成判断等语义状态变化也会重置连续停滞计数。"""
    state = initial_state()
    no_change = record_semantic_progress(
        state,
        assessments=state["assessments"],
        review_blocking_summary=[],
        scope_requests={},
        checkpoint_validation_passed=False,
    )
    assert no_change["stall_count"] == 1

    state["stall_count"] = 1
    first = record_semantic_progress(
        state,
        assessments={"codex": "complete", "claude": "complete"},
        review_blocking_summary=[],
        scope_requests={},
        checkpoint_validation_passed=True,
    )

    assert first["stall_count"] == 0
    unchanged = record_semantic_progress(
        {**state, **first, "made_checkpoint": False},
        assessments={"codex": "complete", "claude": "complete"},
        review_blocking_summary=[],
        scope_requests={},
        checkpoint_validation_passed=True,
    )
    assert unchanged["stall_count"] == 1

    goal_updated = record_semantic_progress(
        {
            **state,
            **unchanged,
            "made_checkpoint": False,
            "goal_contract_version": 2,
        },
        assessments={"codex": "complete", "claude": "complete"},
        review_blocking_summary=[],
        scope_requests={},
        checkpoint_validation_passed=True,
    )
    assert goal_updated["stall_count"] == 0


def test_facilitator_writer_hint_only_applies_after_continue_gate() -> None:
    state = initial_state()
    state["facilitator_next_writer"] = AgentName.CODEX.value
    progress = decide_progress(state)

    assert apply_facilitator_writer_hint(state, progress)["next_writer"] == "codex"

    state["assessments"] = {"codex": "complete", "claude": "complete"}
    final = decide_progress(state)
    assert apply_facilitator_writer_hint(state, final) == final


class BlockingRuntime(ScriptedRuntime):
    """在指定节点注入协议阻断。"""

    def __init__(self, node: str, status: RunStatus) -> None:
        super().__init__()
        self.node = node
        self.blocked_status = status

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        if name == self.node:
            self.calls.append(name)
            return {
                "status": self.blocked_status.value,
                "stop_reason": f"blocked at {name}",
            }
        return super().run_node(name, state)


class AuditDisputeRuntime(ScriptedRuntime):
    """首次否决审计 tree，第二次确认重新生成的候选。"""

    def __init__(self) -> None:
        super().__init__()
        self.tree_ack_count = 0

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        """返回脚本化节点结果；参数为节点/状态，返回更新。"""
        if name == "collect_tree_acks":
            self.calls.append(name)
            self.tree_ack_count += 1
            disputed = self.tree_ack_count == 1
            return {
                "tree_acks_complete": not disputed,
                "audit_dispute": disputed,
            }
        return super().run_node(name, state)


class RecoverableRuntime(ScriptedRuntime):
    """首次 Session 建立失败，恢复后成功的脚本运行时。"""

    def __init__(self) -> None:
        super().__init__()
        self.ensure_attempts = 0

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        """模拟一次可恢复失败；参数为节点/状态，返回节点更新。"""
        if name == "ensure_sessions":
            self.ensure_attempts += 1
            if self.ensure_attempts == 1:
                self.calls.append(name)
                return {
                    "status": RunStatus.FAILED.value,
                    "stop_reason": "temporary CLI failure",
                }
        return super().run_node(name, state)


class RecoverableAuditRuntime(ScriptedRuntime):
    """首次 final tree 确认阻断，恢复后从审计阶段继续。"""

    def __init__(self) -> None:
        super().__init__()
        self.tree_ack_attempts = 0

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        """模拟审计阶段阻断；参数为节点/状态，返回节点更新。"""
        if name == "collect_tree_acks":
            self.calls.append(name)
            self.tree_ack_attempts += 1
            if self.tree_ack_attempts == 1:
                return {
                    "status": RunStatus.INTEGRATION_BLOCKED.value,
                    "stop_reason": "invalid audit tree",
                }
            return {"tree_acks_complete": True, "audit_dispute": False}
        return super().run_node(name, state)


def test_policy_and_integration_blocks_route_to_non_success(tmp_path: Path) -> None:
    for node, status in [
        ("enforce_policy", RunStatus.POLICY_VIOLATION),
        ("integrate_target", RunStatus.INTEGRATION_BLOCKED),
    ]:
        runtime = BlockingRuntime(node, status)
        with SQLiteCheckpoint.open(tmp_path / f"{node}.sqlite") as checkpoint:
            graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
            result = invoke_debate_graph(graph, initial_state(), run_id=f"RUN-{node}")

        assert result["status"] == status.value
        assert "finalize_non_success" in runtime.calls
        assert "cleanup_success" not in runtime.calls


def test_tree_ack_dispute_runs_facilitator_before_regenerating_audit(
    tmp_path: Path,
) -> None:
    runtime = AuditDisputeRuntime()
    with SQLiteCheckpoint.open(tmp_path / "audit-dispute.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        result = invoke_debate_graph(graph, initial_state(), run_id="RUN-AUDIT")

    assert result["status"] == RunStatus.COMPLETED.value
    dispute = runtime.calls.index("facilitator_audit_dispute")
    assert runtime.calls[dispute - 1] == "collect_tree_acks"
    assert runtime.calls[dispute + 1] == "generate_audit_bundle"


def test_terminal_failure_resume_reenters_recovery_and_sessions(tmp_path: Path) -> None:
    """验证已收尾失败可从恢复入口重试；参数为临时目录，无返回值。"""
    runtime = RecoverableRuntime()
    with SQLiteCheckpoint.open(tmp_path / "resume.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        failed = invoke_debate_graph(graph, initial_state(), run_id="RUN-RESUME")

        resumed = resume_debate_graph(graph, run_id="RUN-RESUME")

    assert failed["status"] == RunStatus.FAILED.value
    assert resumed["status"] == RunStatus.COMPLETED.value
    assert "recover_run" in runtime.calls
    assert runtime.ensure_attempts == 2


def test_terminal_audit_failure_resume_skips_completed_debate(tmp_path: Path) -> None:
    """验证审计终态失败恢复时不增加写入轮次；参数为临时目录，无返回值。"""
    runtime = RecoverableAuditRuntime()
    with SQLiteCheckpoint.open(tmp_path / "resume-audit.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        failed = invoke_debate_graph(graph, initial_state(), run_id="RUN-AUDIT-RESUME")
        writer_count = runtime.calls.count("writer_turn")

        resumed = resume_debate_graph(graph, run_id="RUN-AUDIT-RESUME")

    assert failed["status"] == RunStatus.INTEGRATION_BLOCKED.value
    assert resumed["status"] == RunStatus.COMPLETED.value
    assert runtime.calls.count("writer_turn") == writer_count
    recover = runtime.calls.index("recover_run")
    assert runtime.calls[recover + 1] == "generate_audit_bundle"


def test_terminal_stalled_resume_enters_final_checks_when_all_complete(
    tmp_path: Path,
) -> None:
    """验证完成共识的 stalled 检查点恢复后直接进入最终验证。"""
    runtime = ScriptedRuntime()
    state = initial_state()
    state.update(
        {
            "status": RunStatus.STALLED.value,
            "stop_reason": "达到停滞预算",
            "stall_count": state["stall_turns"],
            "assessments": {"codex": "complete", "claude": "complete"},
        }
    )
    with SQLiteCheckpoint.open(tmp_path / "resume-stalled.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        stalled = invoke_debate_graph(graph, state, run_id="RUN-STALLED")
        writer_count = runtime.calls.count("writer_turn")

        resumed = resume_debate_graph(graph, run_id="RUN-STALLED")

    assert stalled["status"] == RunStatus.STALLED.value
    assert resumed["status"] == RunStatus.COMPLETED.value
    assert resumed["stall_count"] == 0
    assert runtime.calls.count("writer_turn") == writer_count
    recover = runtime.calls.index("recover_run")
    assert runtime.calls[recover + 1] == "run_final_validation"


def test_blind_ballots_are_serial_invisible_and_bound_to_readonly_candidate() -> None:
    visible_ballots: list[object] = []
    fingerprint = "quiet"

    def collect(agent: AgentName, candidate: GraphState) -> Ballot:
        visible_ballots.append(candidate.get("ballots"))
        return Ballot(
            agent=agent,
            commit_sha="head",
            goal_contract_version=1,
            completion=CompletionAssessment.COMPLETE,
            criteria=[{"criterion": "goal", "satisfied": True}],
            rationale="complete",
        )

    ballots = collect_blind_ballots(
        state={**initial_state(), "ballots": {"old": {}}},
        current_head="head",
        goal_contract_version=1,
        fingerprint=lambda: fingerprint,
        collector=collect,
    )

    assert visible_ballots == [None, None]
    assert set(ballots) == {"codex", "claude"}

    def mutate(agent: AgentName, candidate: GraphState) -> Ballot:
        nonlocal fingerprint
        fingerprint = f"changed-{agent.value}"
        return collect(agent, candidate)

    with pytest.raises(BlindBallotError, match="只读"):
        collect_blind_ballots(
            state=initial_state(),
            current_head="head",
            goal_contract_version=1,
            fingerprint=lambda: fingerprint,
            collector=mutate,
        )


def test_blind_ballot_runs_optional_semantic_validator_after_binding() -> None:
    """验证终局语义门禁不暴露其他投票，且拒绝不满足显式合同的投票。"""
    seen: list[AgentName] = []

    def collect(agent: AgentName, _candidate: GraphState) -> Ballot:
        return Ballot(
            agent=agent,
            commit_sha="head",
            goal_contract_version=1,
            completion=CompletionAssessment.COMPLETE,
            criteria=[],
            rationale="完成",
        )

    def reject_missing_counterexample(ballot: Ballot) -> None:
        seen.append(ballot.agent)
        ballot.require_counterexample()

    with pytest.raises(ValueError, match="必须提供最强反例"):
        collect_blind_ballots(
            state=initial_state(),
            current_head="head",
            goal_contract_version=1,
            fingerprint=lambda: "quiet",
            collector=collect,
            ballot_validator=reject_missing_counterexample,
        )

    assert seen == [AgentName.CODEX]


def test_sqlite_checkpoint_resumes_from_latest_completed_node(tmp_path: Path) -> None:
    database = tmp_path / "resume.sqlite"
    first_runtime = ScriptedRuntime()
    with SQLiteCheckpoint.open(database) as checkpoint:
        graph = build_debate_graph(
            first_runtime,
            checkpointer=checkpoint.saver,
            interrupt_after=["writer_turn"],
        )
        partial = invoke_debate_graph(graph, initial_state(), run_id="RUN-RESUME")
        assert partial["current_turn"] == 1
        assert "quiesce_workspace" not in first_runtime.calls

    resumed_runtime = ScriptedRuntime()
    with SQLiteCheckpoint.open(database) as checkpoint:
        graph = build_debate_graph(resumed_runtime, checkpointer=checkpoint.saver)
        completed = resume_debate_graph(graph, run_id="RUN-RESUME")

    assert completed["status"] == RunStatus.COMPLETED.value
    assert resumed_runtime.calls[0] == "quiesce_workspace"


class RetryFinalRuntime(ScriptedRuntime):
    """让最终验证或盲投票首次否决候选。"""

    def __init__(self, *, fail_validation: bool) -> None:
        super().__init__()
        self.fail_validation = fail_validation
        self.final_count = 0
        self.ballot_count = 0

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        if name == "run_checkpoint_validation":
            self.calls.append(name)
            return {"checkpoint_validation_passed": True}
        if name == "review_current_head":
            self.calls.append(name)
            return {"assessments": {"codex": "complete", "claude": "complete"}}
        if name == "run_final_validation":
            self.calls.append(name)
            self.final_count += 1
            return {
                "final_validation_passed": not (
                    self.fail_validation and self.final_count == 1
                )
            }
        if name == "collect_blind_ballots":
            self.calls.append(name)
            self.ballot_count += 1
            claude = (
                "incomplete"
                if not self.fail_validation and self.ballot_count == 1
                else "complete"
            )
            return {
                "ballots": {
                    "codex": {"completion": "complete", "commit_sha": "head"},
                    "claude": {"completion": claude, "commit_sha": "head"},
                }
            }
        return super().run_node(name, state)


@pytest.mark.parametrize("fail_validation", [True, False])
def test_final_validation_or_incomplete_ballot_returns_to_writer(
    tmp_path: Path, fail_validation: bool
) -> None:
    runtime = RetryFinalRuntime(fail_validation=fail_validation)
    suffix = "validation" if fail_validation else "ballot"
    with SQLiteCheckpoint.open(tmp_path / f"retry-{suffix}.sqlite") as checkpoint:
        graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
        result = invoke_debate_graph(graph, initial_state(), run_id=f"RUN-{suffix}")

    assert result["status"] == RunStatus.COMPLETED.value
    assert runtime.calls.count("writer_turn") == 2
