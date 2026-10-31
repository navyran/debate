"""LangGraph 节点调度与运行事件发布入口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

from debate_agent.adapters import (
    AdapterOutputError,
)
from debate_agent.artifacts import ArtifactError
from debate_agent.audit import (
    FinalTreeError,
)
from debate_agent.git_workspace import (
    GitWorkspaceError,
    IntegrationBlockedError,
    WorkspaceRaceError,
)
from debate_agent.goal_contract import (
    GoalContractError,
)
from debate_agent.graph import (
    GraphState,
)
from debate_agent.memory import MemoryProtocolError
from debate_agent.models import (
    RunStatus,
)
from debate_agent.policy import PolicyViolationError
from debate_agent.recovery import (
    RecoveryBlockedError,
)
from debate_agent.runtime_backend import _NODE_LABELS, _NODE_PHASES
from debate_agent.runtime_recovery import RecoveryPhaseRuntime
from debate_agent.sessions import SessionIdentityError
from debate_agent.supervisor import LeaseConflictError
from debate_agent.validation import (
    BlockingValidationError,
)


class DebateRuntime(RecoveryPhaseRuntime):
    """按节点名调度全部阶段，并统一归类异常与发布进度。"""

    def run_node(self, name: str, state: GraphState) -> Mapping[str, Any]:
        """执行 LangGraph 节点并归类异常。

        参数为节点名/状态；返回状态更新；已知安全异常转换为明确非完成状态。
        """
        self._latest_state = state
        self._publish_node_event(name=name, state=state, kind="node_started")
        try:
            method = getattr(self, f"_node_{name}")
            result: Mapping[str, Any] = method(state)
        except (
            PolicyViolationError,
            WorkspaceRaceError,
            RecoveryBlockedError,
            IntegrationBlockedError,
            FinalTreeError,
            BlockingValidationError,
            AdapterOutputError,
            ArtifactError,
            FileNotFoundError,
            GitWorkspaceError,
            GoalContractError,
            LeaseConflictError,
            MemoryProtocolError,
            SessionIdentityError,
            ValidationError,
            RuntimeError,
            ValueError,
        ) as exc:
            if self.artifacts.abort_requested():
                result = {
                    "status": RunStatus.ABORTED.value,
                    "stop_reason": "收到 abort 请求",
                }
            else:
                status = RunStatus.FAILED
                if isinstance(exc, PolicyViolationError):
                    status = RunStatus.POLICY_VIOLATION
                elif isinstance(exc, WorkspaceRaceError):
                    status = RunStatus.WORKSPACE_RACE
                elif isinstance(exc, RecoveryBlockedError):
                    status = RunStatus.RECOVERY_BLOCKED
                elif isinstance(exc, (IntegrationBlockedError, FinalTreeError)):
                    status = RunStatus.INTEGRATION_BLOCKED
                elif isinstance(exc, BlockingValidationError):
                    status = (
                        RunStatus.PREFLIGHT_BLOCKED
                        if name == "prepare_environment"
                        else RunStatus.BLOCKED
                    )
                reason = (
                    str(exc)
                    if status is not RunStatus.FAILED
                    else f"{type(exc).__name__}: {str(exc)[:500]}"
                )
                result = {
                    "status": status.value,
                    "stop_reason": reason,
                }
        self._publish_node_event(
            name=name,
            state=cast(GraphState, {**state, **result}),
            kind="node_completed",
        )
        return result

    def _publish_node_event(self, *, name: str, state: GraphState, kind: str) -> None:
        """发布节点进度；参数为节点、状态和事件类型，无返回值，存储失败时抛错。"""
        phase = _NODE_PHASES.get(name, "system")
        actor = "supervisor"
        if name == "writer_turn":
            actor = str(state.get("next_writer", "codex"))
        elif name.startswith("facilitator_"):
            actor = "facilitator"
        elif name in {"collect_blind_ballots", "collect_tree_acks"}:
            actor = "both"
        current_turn = int(state.get("current_turn", 0))
        if name == "writer_turn":
            current_turn += 1
        if name == "writer_turn":
            display_actor = actor.capitalize()
            action = "开始写入" if kind == "node_started" else "完成写入"
            title = f"{display_actor} {action}"
        else:
            label = _NODE_LABELS.get(name, name.replace("_", " "))
            prefix = "开始" if kind == "node_started" else "完成"
            title = f"{prefix}：{label}"
        validation_status = state.get("validation_status")
        if name == "run_checkpoint_validation" and kind == "node_completed":
            passed = state.get("checkpoint_validation_passed")
            validation_status = "passed" if passed else "failed"
        if name == "run_final_validation" and kind == "node_completed":
            passed = state.get("final_validation_passed")
            validation_status = "passed" if passed else "failed"
        self.events.publish(
            kind=kind,
            phase=phase,
            node=name,
            actor=actor,
            title=title,
            current_turn=current_turn,
            max_turns=int(state.get("max_turns", self.config.max_turns)),
            head=state.get("current_head"),
            validation_status=cast(str | None, validation_status),
            status=str(state.get("status", RunStatus.RUNNING.value)),
        )
