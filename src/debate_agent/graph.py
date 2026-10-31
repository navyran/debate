"""LangGraph Debate 控制流、确定性进度门和 SQLite Checkpointer。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from debate_agent.models import (
    AgentName,
    Ballot,
    CompletionAssessment,
    ReviewProtocolVersion,
    RunStatus,
)


class GraphState(TypedDict, total=False):
    """保存图控制所需的小型 JSON 状态和运行期引用。"""

    run_id: str
    status: str
    stop_reason: str | None
    current_turn: int
    max_turns: int
    failure_count: int
    max_process_failures: int
    stall_count: int
    stall_turns: int
    participants: list[str]
    next_writer: str
    current_writer: str
    assessments: dict[str, str]
    checkpoint_validation_passed: bool
    final_validation_passed: bool
    ballots: dict[str, dict[str, Any]]
    tree_acks_complete: bool
    audit_dispute: bool
    integrate_to_target: bool
    integration_succeeded: bool
    integration_skipped: bool
    resume_requested: bool
    current_head: str
    session_generations: dict[str, int]
    gate: str
    repo_root: str
    target_branch: str
    base_commit: str
    worktree_path: str
    debate_branch: str
    goal_contract_ref: str
    goal_contract_version: int
    lease: dict[str, Any] | None
    turn_result: dict[str, Any]
    review_result: dict[str, Any]
    review_result_ref: str | None
    review_blocking_summary: list[str]
    review_protocol_version: str
    validated_paths: list[str]
    validation_report_ref: str
    last_commit: str
    latest_change_summary_ref: str
    pending_update_refs: dict[str, list[str]]
    scope_requests: dict[str, list[str]]
    final_tree_sha: str
    integrated_tree_sha: str
    tree_acks: dict[str, dict[str, Any]]
    integration_commit: str
    result_branch: str
    result_worktree: str
    workspace_snapshot: dict[str, Any]
    current_invocation_id: str
    made_checkpoint: bool
    final_candidate: dict[str, Any]
    audit_path: str
    facilitator_decision_ref: str | None
    facilitator_instruction_ref: str | None
    facilitator_status: str | None
    facilitator_mode: str | None
    facilitator_next_writer: str | None
    active_elapsed_seconds: float
    semantic_progress_signature: str


class GraphRuntime(Protocol):
    """声明外部副作用节点必须实现的统一运行时接口。"""

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        """执行节点；参数为节点名和状态，返回小型状态更新，异常由运行时归类。"""
        raise RuntimeError("GraphRuntime 必须实现 run_node")


class BlindBallotError(RuntimeError):
    """表示盲投票身份、候选绑定或只读边界被违反。"""


REQUIRED_NODE_NAMES = frozenset(
    {
        "preflight",
        "acquire_repo_lock",
        "create_worktree",
        "prepare_environment",
        "build_goal_contract",
        "ensure_sessions",
        "sync_peer_memory",
        "collect_change_ack",
        "grant_write_lease",
        "writer_turn",
        "quiesce_workspace",
        "enforce_policy",
        "run_checkpoint_validation",
        "commit_checkpoint",
        "publish_handoff",
        "review_current_head",
        "update_goal_contract",
        "facilitator_assess",
        "progress_gate",
        "run_final_validation",
        "collect_blind_ballots",
        "generate_audit_bundle",
        "facilitator_audit_dispute",
        "materialize_final_tree",
        "collect_tree_acks",
        "create_integration_commit",
        "integrate_target",
        "preserve_result",
        "verify_integration",
        "cleanup_success",
        "recover_run",
        "finalize_non_success",
    }
)


@dataclass(slots=True)
class SQLiteCheckpoint:
    """持有 SQLite 连接和 LangGraph Saver 的显式生命周期。"""

    connection: sqlite3.Connection
    saver: SqliteSaver

    @classmethod
    def open(cls, path: Path) -> SQLiteCheckpoint:
        """打开 SQLite Checkpointer；参数为文件路径，返回资源，连接失败时抛错。"""
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(resolved, check_same_thread=False)
        return cls(connection=connection, saver=SqliteSaver(connection))

    def close(self) -> None:
        """提交并关闭连接；无参数和返回值，SQLite 失败时抛错。"""
        self.connection.commit()
        self.connection.close()

    def __enter__(self) -> SQLiteCheckpoint:
        """进入资源上下文；无参数，返回自身，不抛异常。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出资源上下文；参数为异常信息，无返回值，关闭连接。"""
        self.close()


def _is_running(state: GraphState) -> bool:
    """判断状态仍可执行；参数为图状态，返回布尔值，不抛异常。"""
    return state.get("status", RunStatus.RUNNING.value) == RunStatus.RUNNING.value


def _participants(state: GraphState) -> tuple[AgentName, ...]:
    """读取本次参与者；参数为状态，返回稳定 Agent 元组，历史状态默认双 Agent。"""
    values = state.get(
        "participants", [AgentName.CODEX.value, AgentName.CLAUDE.value]
    )
    return tuple(AgentName(value) for value in values)


def _both_complete(state: GraphState) -> bool:
    """判断所有参与者过程评估均完成；参数为状态，返回布尔值，不抛异常。"""
    assessments = state.get("assessments", {})
    return all(
        assessments.get(agent.value) == CompletionAssessment.COMPLETE.value
        for agent in _participants(state)
    )


def _has_review_blockers(state: GraphState) -> bool:
    """判断审查协议仍有阻断项；参数为图状态，返回布尔值，不抛异常。"""
    return bool(
        state.get("review_protocol_version")
        == ReviewProtocolVersion.REVIEW_V1.value
        and state.get("review_blocking_summary")
    )


def _ready_for_final(state: GraphState) -> bool:
    """判断过程共识可进入最终确认；参数为图状态，返回布尔值，不抛异常。"""
    return _both_complete(state) and not _has_review_blockers(state)


def record_semantic_progress(
    state: GraphState,
    *,
    assessments: Mapping[str, str],
    review_blocking_summary: list[str],
    scope_requests: Mapping[str, list[str]],
    checkpoint_validation_passed: bool,
) -> GraphState:
    """记录连续停滞所依据的语义状态。

    参数为旧状态、完成评估、阻断项、范围请求和验证结果；返回停滞计数及签名。
    Git 提交或语义签名变化会清零连续停滞；输入不可序列化时抛出 JSON 异常。
    """
    payload = {
        "current_head": state.get("current_head"),
        "goal_contract_version": state.get("goal_contract_version", 0),
        "assessments": dict(sorted(assessments.items())),
        "review_blocking_summary": sorted(review_blocking_summary),
        "scope_requests": {
            agent: sorted(paths) for agent, paths in sorted(scope_requests.items())
        },
        "checkpoint_validation_passed": checkpoint_validation_passed,
    }
    signature = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    previous_signature = state.get("semantic_progress_signature")
    first_review_changed = (
        dict(assessments) != state.get("assessments", {})
        or sorted(review_blocking_summary)
        != sorted(state.get("review_blocking_summary", []))
        or {
            agent: sorted(paths) for agent, paths in sorted(scope_requests.items())
        }
        != {
            agent: sorted(paths)
            for agent, paths in sorted(state.get("scope_requests", {}).items())
        }
        or checkpoint_validation_passed
        != bool(state.get("checkpoint_validation_passed", False))
    )
    progressed = (
        state.get("made_checkpoint", False)
        or (
            first_review_changed
            if previous_signature is None
            else previous_signature != signature
        )
    )
    return {
        "stall_count": 0 if progressed else state.get("stall_count", 0) + 1,
        "semantic_progress_signature": signature,
    }


def _choose_next_writer(state: GraphState) -> str:
    """选择异议 Writer；参数为状态，返回 Agent 名，不替代业务判断。"""
    assessments = state.get("assessments", {})
    incomplete = [
        agent.value
        for agent in _participants(state)
        if assessments.get(agent.value) != CompletionAssessment.COMPLETE.value
    ]
    if len(incomplete) == 1:
        return incomplete[0]
    participants = _participants(state)
    current = AgentName(
        state.get("current_writer", state.get("next_writer", participants[0].value))
    )
    current_index = participants.index(current) if current in participants else -1
    for offset in range(1, len(participants) + 1):
        candidate = participants[(current_index + offset) % len(participants)]
        if candidate.value in incomplete:
            return candidate.value
    return incomplete[0]


def decide_progress(state: GraphState) -> GraphState:
    """执行确定性进度门。

    参数为当前状态；返回 gate/下一 Writer 或非完成状态；不生成业务结论。
    """
    if not _is_running(state):
        return {"gate": "stop"}
    if state.get("failure_count", 0) >= state.get("max_process_failures", 2):
        return {
            "gate": "stop",
            "status": RunStatus.FAILED.value,
            "stop_reason": "达到进程失败预算",
        }
    if _ready_for_final(state):
        return {"gate": "final"}
    if state.get("current_turn", 0) >= state.get("max_turns", 1):
        return {
            "gate": "stop",
            "status": RunStatus.MAX_TURNS.value,
            "stop_reason": "达到最大写入回合数",
        }
    if (
        not _both_complete(state)
        and state.get("stall_count", 0) >= state.get("stall_turns", 2)
    ):
        return {
            "gate": "stop",
            "status": RunStatus.STALLED.value,
            "stop_reason": "达到停滞预算",
        }
    if _has_review_blockers(state):
        return {"gate": "continue", "next_writer": _choose_next_writer(state)}
    return {"gate": "continue", "next_writer": _choose_next_writer(state)}


def apply_facilitator_writer_hint(
    state: GraphState, progress: GraphState
) -> GraphState:
    """应用已校验的 Writer 提示；参数为状态/确定性结果，返回安全路由更新。"""
    if progress.get("gate") != "continue":
        return progress
    writer = state.get("facilitator_next_writer")
    if writer not in {agent.value for agent in _participants(state)}:
        return progress
    return cast(GraphState, {**progress, "next_writer": writer})


def collect_blind_ballots(
    *,
    state: GraphState,
    current_head: str,
    goal_contract_version: int,
    fingerprint: Callable[[], str],
    collector: Callable[[AgentName, GraphState], Ballot],
    ballot_validator: Callable[[Ballot], None] | None = None,
    participants: tuple[AgentName, ...] = (
        AgentName.CODEX,
        AgentName.CLAUDE,
    ),
) -> dict[str, dict[str, Any]]:
    """串行收集相互不可见的盲投票。

    参数为固定候选、HEAD、契约版本、指纹、收集器和可选语义校验器；返回投票；写入、
    绑定或语义校验错误时抛错。校验器只读取已解析投票，不能看到其他参与者的投票。
    """
    baseline = fingerprint()
    ballots: dict[str, dict[str, Any]] = {}
    candidate = cast(
        GraphState, {key: value for key, value in state.items() if key != "ballots"}
    )
    for agent in participants:
        if fingerprint() != baseline:
            raise BlindBallotError("盲投票前只读候选已变化")
        ballot = collector(agent, copy.deepcopy(candidate))
        if fingerprint() != baseline:
            raise BlindBallotError("盲投票只读阶段检测到写入")
        if (
            ballot.agent is not agent
            or ballot.commit_sha != current_head
            or ballot.goal_contract_version != goal_contract_version
        ):
            raise BlindBallotError("盲投票未绑定当前候选或 GoalContract")
        if ballot_validator is not None:
            ballot_validator(ballot)
        ballots[agent.value] = ballot.model_dump(mode="json")
    return ballots


def _runtime_node(runtime: GraphRuntime, name: str) -> Any:
    """包装普通 runtime 节点；参数为 runtime/名称，返回 LangGraph 节点函数。"""

    def node(state: GraphState) -> GraphState:
        """执行一个外部节点；参数为状态，返回更新，异常由 runtime 负责归类。"""
        updates = dict(runtime.run_node(name, state))
        if name == "writer_turn":
            updates.setdefault("current_writer", state.get("next_writer", "codex"))
            updates.setdefault("current_turn", state.get("current_turn", 0) + 1)
        if name == "run_final_validation" and not updates.get(
            "final_validation_passed", False
        ):
            updates.setdefault(
                "assessments",
                {
                    agent.value: CompletionAssessment.INCOMPLETE.value
                    for agent in _participants(state)
                },
            )
        if name == "collect_blind_ballots":
            ballots = updates.get("ballots", {})
            if any(
                ballot.get("completion") != CompletionAssessment.COMPLETE.value
                for ballot in ballots.values()
            ):
                updates["assessments"] = {
                    agent.value: ballots.get(agent.value, {}).get(
                        "completion", CompletionAssessment.INCOMPLETE.value
                    )
                    for agent in _participants(state)
                }
                updates["final_validation_passed"] = False
        if (
            name == "integrate_target"
            and not updates.get("integration_succeeded", False)
            and updates.get("status", state.get("status")) == RunStatus.RUNNING.value
        ):
            updates.update(
                {
                    "status": RunStatus.INTEGRATION_BLOCKED.value,
                    "stop_reason": "目标分支集成未成功",
                }
            )
        if (
            name == "cleanup_success"
            and updates.get("status", state.get("status")) == RunStatus.RUNNING.value
        ):
            updates["status"] = RunStatus.COMPLETED.value
            updates["stop_reason"] = None
        return cast(GraphState, updates)

    return node


def _progress_node(runtime: GraphRuntime) -> Any:
    """包装 progress gate；参数为 runtime，返回确定性 LangGraph 节点。"""

    def node(state: GraphState) -> GraphState:
        """合并 runtime 计数并决定下一边；参数为状态，返回小型更新。"""
        updates = dict(runtime.run_node("progress_gate", state))
        combined = cast(GraphState, {**state, **updates})
        progress = decide_progress(combined)
        updates.update(apply_facilitator_writer_hint(combined, progress))
        return cast(GraphState, updates)

    return node


def _finalize_node(runtime: GraphRuntime) -> Any:
    """包装非成功收尾；参数为 runtime，返回保证 stop reason 的节点。"""

    def node(state: GraphState) -> GraphState:
        """保存非成功状态；参数为状态，返回更新，不把状态误判为 completed。"""
        updates = dict(runtime.run_node("finalize_non_success", state))
        combined = {**state, **updates}
        if combined.get("status") == RunStatus.RUNNING.value:
            updates["status"] = RunStatus.FAILED.value
        if not combined.get("stop_reason"):
            updates["stop_reason"] = "运行未完成"
        return cast(GraphState, updates)

    return node


def _active_route(state: GraphState) -> str:
    """路由普通节点；参数为状态，返回 active/stop，不抛异常。"""
    return "active" if _is_running(state) else "stop"


def _start_route(state: GraphState) -> str:
    """选择新运行或恢复；参数为状态，返回路由键，不抛异常。"""
    return "recover" if state.get("resume_requested", False) else "new"


def _gate_route(state: GraphState) -> str:
    """读取进度门路由；参数为状态，返回 continue/final/stop。"""
    return state.get("gate", "stop")


def _final_validation_route(state: GraphState) -> str:
    """路由最终验证；参数为状态，返回 ballots/continue/stop。"""
    if not _is_running(state):
        return "stop"
    return "ballots" if state.get("final_validation_passed", False) else "continue"


def _ballot_route(state: GraphState) -> str:
    """路由盲投票；参数为状态，仅所有参与者 complete 时返回 audit。"""
    if not _is_running(state):
        return "stop"
    ballots = state.get("ballots", {})
    current_head = state.get("current_head")
    participants = {agent.value for agent in _participants(state)}
    accepted = set(ballots) == participants and all(
        ballot.get("completion") == CompletionAssessment.COMPLETE.value
        and (current_head is None or ballot.get("commit_sha") == current_head)
        for ballot in ballots.values()
    )
    return "audit" if accepted else "continue"


def _tree_ack_route(state: GraphState) -> str:
    """路由 final tree 确认；参数为状态，返回 integrate/audit/stop。"""
    if not _is_running(state):
        return "stop"
    if state.get("tree_acks_complete", False):
        return "integrate"
    if state.get("audit_dispute", False):
        return "audit"
    return "stop"


def _recover_route(state: GraphState) -> str:
    """路由终态恢复；参数为状态，返回 sessions/final/audit/stop。"""
    if not _is_running(state):
        return "stop"
    if state.get("final_validation_passed", False) and _ballot_route(state) == "audit":
        return "audit"
    if _ready_for_final(state):
        return "final"
    return "sessions"


def _integration_choice_route(state: GraphState) -> str:
    """选择最终交付方式；参数为冻结状态，返回 integrate/preserve。"""
    return "integrate" if state.get("integrate_to_target", True) else "preserve"


def build_debate_graph(
    runtime: GraphRuntime,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    interrupt_after: list[str] | None = None,
) -> Any:
    """构建完整 Debate 图。

    参数为外部 runtime 和可选 Checkpointer；返回编译图；LangGraph 配置错误时抛错。
    """
    builder = StateGraph(GraphState)
    for name in REQUIRED_NODE_NAMES:
        if name == "progress_gate":
            builder.add_node(name, _progress_node(runtime))
        elif name == "finalize_non_success":
            builder.add_node(name, _finalize_node(runtime))
        else:
            builder.add_node(name, _runtime_node(runtime, name))

    builder.add_conditional_edges(
        START,
        _start_route,
        {"new": "preflight", "recover": "recover_run"},
    )
    linear_edges = [
        ("preflight", "acquire_repo_lock"),
        ("acquire_repo_lock", "create_worktree"),
        ("create_worktree", "prepare_environment"),
        ("prepare_environment", "build_goal_contract"),
        ("build_goal_contract", "ensure_sessions"),
        ("ensure_sessions", "sync_peer_memory"),
        ("sync_peer_memory", "collect_change_ack"),
        ("collect_change_ack", "grant_write_lease"),
        ("grant_write_lease", "writer_turn"),
        ("writer_turn", "quiesce_workspace"),
        ("quiesce_workspace", "enforce_policy"),
        ("enforce_policy", "run_checkpoint_validation"),
        ("run_checkpoint_validation", "commit_checkpoint"),
        ("commit_checkpoint", "publish_handoff"),
        ("publish_handoff", "review_current_head"),
        ("review_current_head", "update_goal_contract"),
        ("update_goal_contract", "facilitator_assess"),
        ("facilitator_assess", "progress_gate"),
        ("generate_audit_bundle", "materialize_final_tree"),
        ("facilitator_audit_dispute", "generate_audit_bundle"),
        ("materialize_final_tree", "collect_tree_acks"),
        ("integrate_target", "verify_integration"),
        ("verify_integration", "cleanup_success"),
        ("preserve_result", "cleanup_success"),
    ]
    for source, target in linear_edges:
        builder.add_conditional_edges(
            source,
            _active_route,
            {"active": target, "stop": "finalize_non_success"},
        )
    builder.add_conditional_edges(
        "recover_run",
        _recover_route,
        {
            "sessions": "ensure_sessions",
            "final": "run_final_validation",
            "audit": "generate_audit_bundle",
            "stop": "finalize_non_success",
        },
    )
    builder.add_conditional_edges(
        "progress_gate",
        _gate_route,
        {
            "continue": "sync_peer_memory",
            "final": "run_final_validation",
            "stop": "finalize_non_success",
        },
    )
    builder.add_conditional_edges(
        "run_final_validation",
        _final_validation_route,
        {
            "ballots": "collect_blind_ballots",
            "continue": "progress_gate",
            "stop": "finalize_non_success",
        },
    )
    builder.add_conditional_edges(
        "collect_blind_ballots",
        _ballot_route,
        {
            "audit": "generate_audit_bundle",
            "continue": "progress_gate",
            "stop": "finalize_non_success",
        },
    )
    builder.add_conditional_edges(
        "collect_tree_acks",
        _tree_ack_route,
        {
            "integrate": "create_integration_commit",
            "audit": "facilitator_audit_dispute",
            "stop": "finalize_non_success",
        },
    )
    builder.add_conditional_edges(
        "create_integration_commit",
        _integration_choice_route,
        {"integrate": "integrate_target", "preserve": "preserve_result"},
    )
    builder.add_edge("cleanup_success", END)
    builder.add_edge("finalize_non_success", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_after=interrupt_after,
    )


def invoke_debate_graph(graph: Any, state: GraphState, *, run_id: str) -> GraphState:
    """以 run ID 作为 thread ID 执行图。

    参数为编译图、初始状态和 run ID；返回最终状态；图执行错误时传播异常。
    """
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
    return cast(GraphState, graph.invoke(state, config=config))


def resume_debate_graph(graph: Any, *, run_id: str) -> GraphState:
    """从最新 SQLite checkpoint 继续图。

    参数为编译图和原 run ID；返回最终状态；没有可恢复 checkpoint 时传播异常。
    """
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
    snapshot = graph.get_state(config)
    if snapshot.next:
        return cast(GraphState, graph.invoke(None, config=config))
    state = cast(GraphState, dict(snapshot.values))
    was_stalled = state.get("status") == RunStatus.STALLED.value
    state.update(
        {
            "resume_requested": True,
            "status": RunStatus.RUNNING.value,
            "stop_reason": None,
            **({"stall_count": 0} if was_stalled else {}),
        }
    )
    return cast(GraphState, graph.invoke(state, config=config))
