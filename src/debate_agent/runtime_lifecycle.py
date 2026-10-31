"""Debate Run 的启动、恢复、中止和清理生命周期。"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from debate_agent.agent_catalog import agent_definition
from debate_agent.artifacts import ArtifactStore
from debate_agent.cli import LifecycleService
from debate_agent.cli_health import (
    CliPreflightError,
    preflight_run_config,
    rebuild_resume_config,
)
from debate_agent.config import RunConfig
from debate_agent.git_workspace import (
    GitWorkspace,
)
from debate_agent.graph import (
    GraphState,
    SQLiteCheckpoint,
    build_debate_graph,
    invoke_debate_graph,
    resume_debate_graph,
)
from debate_agent.models import (
    CompletionAssessment,
    RunStatus,
)
from debate_agent.recovery import (
    RecoveryBlockedError,
)
from debate_agent.runtime_backend import (
    BackendFactory,
    ConfigPreflight,
    FacilitatorFactory,
    ResumeConfigPreflight,
)
from debate_agent.runtime_engine import DebateRuntime


class DebateLifecycleService(LifecycleService):
    """实现 CLI run/resume/abort/cleanup 生命周期。"""

    def __init__(
        self,
        *,
        backend_factory: BackendFactory | None = None,
        facilitator_factory: FacilitatorFactory | None = None,
        config_preflight: ConfigPreflight | None = None,
        resume_config_preflight: ResumeConfigPreflight | None = None,
    ) -> None:
        """创建服务。

        参数为 CLI/Facilitator 工厂、新运行预检和恢复预检；返回实例。注入 Backend
        的测试路径默认不执行真实恢复预检，生产路径会在恢复 OpenCode 时重建私有能力。
        """
        self.backend_factory = backend_factory
        self.facilitator_factory = facilitator_factory
        self.config_preflight = (
            config_preflight
            if config_preflight is not None
            else preflight_run_config
            if backend_factory is None
            else None
        )
        self.resume_config_preflight = (
            resume_config_preflight
            if resume_config_preflight is not None
            else rebuild_resume_config
            if backend_factory is None
            else None
        )

    @staticmethod
    def new_run_id() -> str:
        """生成可排序 run ID；无参数，返回安全标识，不抛异常。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        entropy = hashlib.sha256(
            f"{time.time_ns()}-{os.getpid()}".encode()
        ).hexdigest()[:8]
        return f"RUN-{stamp}-{entropy}"

    def _runtime(
        self, config: RunConfig, run_id: str, artifacts: ArtifactStore
    ) -> DebateRuntime:
        """创建节点 runtime；参数为配置/run/artifacts，返回实例。"""
        backend = (
            self.backend_factory(config, artifacts) if self.backend_factory else None
        )
        facilitator = (
            self.facilitator_factory(config, artifacts)
            if self.facilitator_factory
            else None
        )
        return DebateRuntime(
            config=config,
            run_id=run_id,
            artifacts=artifacts,
            backend=backend,
            facilitator=facilitator,
        )

    @staticmethod
    def _result(state: GraphState, run_id: str) -> dict[str, object]:
        """生成 CLI 结果；参数为状态/run ID，返回简洁字典。"""
        return {
            "run_id": run_id,
            "status": state.get("status", RunStatus.FAILED.value),
            "stop_reason": state.get("stop_reason"),
            "current_head": state.get("current_head"),
            "final_tree_sha": state.get("final_tree_sha"),
            "integration_succeeded": state.get("integration_succeeded", False),
            "integration_skipped": state.get("integration_skipped", False),
            "result_branch": state.get("result_branch"),
            "result_worktree": state.get("result_worktree"),
        }

    def run(self, config: RunConfig) -> dict[str, object]:
        """启动新 run；参数为配置，返回终态，图/文件错误时传播异常。"""
        return self.run_with_id(config, run_id=self.new_run_id())

    def run_with_id(self, config: RunConfig, *, run_id: str) -> dict[str, object]:
        """执行预分配 run；参数为配置/run ID，返回终态，身份冲突时抛错。"""
        original_config = config
        if self.config_preflight is not None:
            config = self.config_preflight(config)
        existing = ArtifactStore(state_root=config.state_root, run_id=run_id)
        if existing.config_path.exists() and config != original_config:
            existing.replace_config_before_execution(
                expected=original_config,
                replacement=config,
            )
        artifacts = ArtifactStore.create(config, run_id=run_id)
        runtime = self._runtime(config, run_id, artifacts)
        initial: GraphState = {
            "run_id": run_id,
            "status": RunStatus.RUNNING.value,
            "stop_reason": None,
            "current_turn": 0,
            "max_turns": config.max_turns,
            "failure_count": 0,
            "max_process_failures": config.max_process_failures,
            "stall_count": 0,
            "stall_turns": config.stall_turns,
            "participants": [agent.value for agent in config.participants],
            "review_protocol_version": config.protocol_version.value,
            "review_result_ref": None,
            "review_blocking_summary": [],
            "next_writer": config.first_agent.value,
            "assessments": {
                agent.value: CompletionAssessment.INCOMPLETE.value
                for agent in config.participants
            },
            "scope_requests": {},
            "resume_requested": False,
            "integrate_to_target": config.integrate_to_target,
            "active_elapsed_seconds": 0.0,
        }
        checkpoint_path = artifacts.run_dir / "checkpoint.sqlite"
        with SQLiteCheckpoint.open(checkpoint_path) as checkpoint:
            graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
            final = invoke_debate_graph(graph, initial, run_id=run_id)
        artifacts.write_state(final)
        if final.get("status") == RunStatus.COMPLETED.value:
            artifacts.write_status(RunStatus.COMPLETED, stop_reason=None)
        return self._result(final, run_id)

    def resume(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """从最新 checkpoint 恢复；参数为状态根/run ID，返回终态，不重放已保存结果。"""
        artifacts = ArtifactStore.open(state_root=state_root, run_id=run_id)
        persisted = artifacts.read_status()
        if persisted.get("status") == RunStatus.COMPLETED.value:
            return {
                "run_id": run_id,
                "status": RunStatus.COMPLETED.value,
                "stop_reason": None,
            }
        config = artifacts.load_config()
        checkpoint_path = artifacts.run_dir / "checkpoint.sqlite"
        if not checkpoint_path.is_file():
            return self.run_with_id(config, run_id=run_id)
        requires_resume_preflight = any(
            agent_definition(agent).freezes_runtime_config
            for agent in config.participants
        )
        if self.resume_config_preflight is not None and requires_resume_preflight:
            try:
                config = self.resume_config_preflight(config)
            except CliPreflightError as exc:
                artifacts.write_status(
                    RunStatus.RECOVERY_BLOCKED,
                    stop_reason=str(exc),
                )
                return {
                    "run_id": run_id,
                    "status": RunStatus.RECOVERY_BLOCKED.value,
                    "stop_reason": str(exc),
                }
        runtime = self._runtime(config, run_id, artifacts)
        artifacts.clear_abort_request()
        artifacts.write_status(RunStatus.RUNNING, stop_reason=None)
        with SQLiteCheckpoint.open(checkpoint_path) as checkpoint:
            graph = build_debate_graph(runtime, checkpointer=checkpoint.saver)
            try:
                runtime.recover_before_resume(graph)
            except RecoveryBlockedError as exc:
                artifacts.write_status(
                    RunStatus.RECOVERY_BLOCKED,
                    stop_reason=str(exc),
                )
                return {
                    "run_id": run_id,
                    "status": RunStatus.RECOVERY_BLOCKED.value,
                    "stop_reason": str(exc),
                }
            final = resume_debate_graph(graph, run_id=run_id)
        artifacts.write_state(final)
        return self._result(final, run_id)

    def abort(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """请求中止并保留资源；参数为状态根/run ID，返回 aborted 状态。"""
        artifacts = ArtifactStore.open(state_root=state_root, run_id=run_id)
        persisted = artifacts.read_status()
        if persisted.get("status") == RunStatus.COMPLETED.value:
            return {
                "run_id": run_id,
                "status": RunStatus.COMPLETED.value,
                "stop_reason": None,
            }
        artifacts.request_abort()
        artifacts.write_status(RunStatus.ABORTED, stop_reason="用户请求 abort")
        return {
            "run_id": run_id,
            "status": RunStatus.ABORTED.value,
            "stop_reason": "用户请求 abort",
        }

    def cleanup(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """显式清理指定 run；参数为状态根/run ID，返回 cleaned，运行中时拒绝。"""
        artifacts = ArtifactStore.open(state_root=state_root, run_id=run_id)
        status = artifacts.read_status().get("status")
        if status == RunStatus.RUNNING.value:
            raise RuntimeError("运行中不能 cleanup；请先 abort")
        record = artifacts.read_repo()
        if record:
            workspace = GitWorkspace.from_record(
                record,
                expected_state_root=artifacts.state_root,
                expected_run_id=run_id,
                require_worktree=False,
            )
            workspace.cleanup_success()
        artifacts.cleanup_sensitive()
        return {"run_id": run_id, "status": "cleaned"}
