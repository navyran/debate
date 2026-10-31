"""Debate Runtime 的共享状态、CLI 调用和基础协作能力。"""

from __future__ import annotations

import hashlib
import os
import re
import time

from debate_agent.adapters import (
    AdapterOutputError,
    SessionBoundaryError,
)
from debate_agent.agent_catalog import agent_definition
from debate_agent.artifacts import ArtifactError, ArtifactStore
from debate_agent.cli_health import (
    CircuitBreakerStore,
    CliIdentity,
    FailureCategory,
    FailureClassifier,
)
from debate_agent.config import RunConfig
from debate_agent.events import RunEventStore
from debate_agent.facilitator import FacilitatorService
from debate_agent.git_workspace import (
    GitWorkspace,
    WorkspaceRaceError,
)
from debate_agent.goal_contract import (
    GoalContract,
    GoalContractStore,
)
from debate_agent.graph import (
    GraphState,
)
from debate_agent.memory import MemoryProtocolError, MemoryStore
from debate_agent.models import (
    AgentName,
    ChangeAck,
    PeerMemoryUpdate,
    RunStatus,
    SessionHealth,
)
from debate_agent.policy import CommandPolicy, WritePolicy
from debate_agent.process import (
    ProcessRunner,
)
from debate_agent.prompts import (
    build_peer_memory_prompt,
    build_session_bootstrap_prompt,
)
from debate_agent.runtime_backend import (
    AgentBackend,
    CLIBackend,
    FacilitatorBackend,
    ResponseModel,
)
from debate_agent.sessions import SessionManager
from debate_agent.supervisor import Supervisor
from debate_agent.validation import (
    ValidationRunner,
)


class RuntimeBase:
    """实现所有 LangGraph 节点的确定性外部副作用。"""

    def __init__(
        self,
        *,
        config: RunConfig,
        run_id: str,
        artifacts: ArtifactStore,
        backend: AgentBackend | None = None,
        facilitator: FacilitatorBackend | None = None,
    ) -> None:
        """创建 runtime；参数为配置/run/artifacts/可选 backend，返回实例。"""
        self.config = config
        self.run_id = run_id
        self.artifacts = artifacts
        self.events = RunEventStore(state_root=config.state_root, run_id=run_id)
        approved = (*config.setup_commands, *config.validation_commands)
        self.command_policy = CommandPolicy(
            approved_commands=approved,
            environment_allowlist=(
                *config.environment_allowlist,
                *config.proxy_environment_allowlist,
            ),
            network_enabled=config.allow_network,
            unrestricted_commands=config.unrestricted_commands,
        )
        self.env = self.command_policy.filter_environment(os.environ)
        self.backend = backend or CLIBackend(
            config=config,
            artifacts=artifacts,
            command_policy=self.command_policy,
            env=self.env,
        )
        self.facilitator = facilitator or FacilitatorService(
            config=config,
            artifacts=artifacts,
            environment=os.environ,
        )
        self.workspace: GitWorkspace | None = None
        self.supervisor: Supervisor | None = None
        self.sessions: SessionManager | None = None
        self.memory: MemoryStore | None = None
        self.contract_store = GoalContractStore(
            state_root=config.state_root, run_id=run_id
        )
        self.validation_runner: ValidationRunner | None = None
        self._last_invocation_id: str | None = None
        self._active_segment_started = time.monotonic()
        self._active_elapsed_baseline: float | None = None
        self._latest_state: GraphState = {
            "active_elapsed_seconds": 0.0,
            "status": RunStatus.RUNNING.value,
        }
        self.failure_classifier = FailureClassifier()
        self.circuit_breaker = CircuitBreakerStore(
            state_root=config.state_root,
            threshold=config.circuit_breaker_threshold,
            window_seconds=config.circuit_breaker_window_seconds,
            cooldown_seconds=config.circuit_breaker_cooldown_seconds,
        )
        if isinstance(self.backend, CLIBackend):
            self.backend.remaining_run_seconds = self._remaining_run_seconds
            self.backend.goal_contract_version = lambda: self._contract().version

    def _active_elapsed_seconds(self, state: GraphState) -> float:
        """计算累计活跃执行时长；参数为图状态，返回秒数，不计两次运行间的离线时间。"""
        if self._active_elapsed_baseline is None:
            persisted = state.get("active_elapsed_seconds", 0.0)
            self._active_elapsed_baseline = max(float(persisted), 0.0)
        return self._active_elapsed_baseline + max(
            time.monotonic() - self._active_segment_started,
            0.0,
        )

    def _remaining_run_seconds(self) -> float:
        """计算 Run 剩余活跃预算；无参数，返回秒数，不抛异常。"""
        return self.config.total_timeout_seconds - self._active_elapsed_seconds(
            self._latest_state
        )

    @staticmethod
    def _is_retryable_failure(category: FailureCategory) -> bool:
        """判断基础设施失败能否安全重试；参数为类别，返回布尔值。"""
        return category in {
            FailureCategory.STARTUP_TIMEOUT,
            FailureCategory.IDLE_TIMEOUT,
            FailureCategory.NETWORK_ERROR,
            FailureCategory.PROCESS_ERROR,
        }

    def _cli_identity(self, agent: AgentName) -> CliIdentity | None:
        """读取冻结 CLI 身份。

        参数为 Agent；返回可用于熔断的身份或 None；历史 Run 缺字段时安全降级。
        """
        profile = self.config.profiles[agent]
        if not profile.version or not profile.model:
            return None
        definition = agent_definition(agent)
        return CliIdentity(
            agent=agent,
            executable=profile.command,
            version=profile.version,
            model=profile.model,
            variant=profile.variant if definition.supports_variant else None,
            runtime_config_hash=(
                profile.runtime_config_hash
                if definition.freezes_runtime_config
                else None
            ),
            runtime_config_content_hash=(
                profile.runtime_config_content_hash
                if definition.freezes_runtime_config
                else None
            ),
            runtime_config_resolved_hash=(
                profile.runtime_config_resolved_hash
                if definition.freezes_runtime_config
                else None
            ),
        )

    def _ensure_workspace(self) -> GitWorkspace:
        """加载共享 worktree；无参数，返回 GitWorkspace，记录缺失时抛错。"""
        if self.workspace is None:
            self.workspace = GitWorkspace.from_record(
                self.artifacts.read_repo(),
                expected_state_root=self.config.state_root,
                expected_run_id=self.run_id,
            )
        return self.workspace

    def _ensure_components(self) -> None:
        """初始化 worktree 依赖组件；无参数和返回值，路径/状态错误时抛错。"""
        workspace = self._ensure_workspace()
        if self.supervisor is None:
            self.supervisor = Supervisor(
                state_root=self.config.state_root, run_id=self.run_id
            )
        if self.sessions is None:
            self.sessions = SessionManager.open(
                state_root=self.config.state_root,
                run_id=self.run_id,
                workspace=workspace.worktree_path,
                profiles={
                    agent: self.config.profiles[agent]
                    for agent in self.config.participants
                },
            )
        if self.memory is None:
            self.memory = MemoryStore(
                state_root=self.config.state_root,
                run_id=self.run_id,
                participants=self.config.participants,
            )
        if self.validation_runner is None:
            self.validation_runner = ValidationRunner(
                state_root=self.config.state_root,
                run_id=self.run_id,
                process_runner=ProcessRunner(raw_root=self.artifacts.run_dir / "raw"),
                command_policy=self.command_policy,
                artifact_store=self.artifacts,
            )

    def _contract(self) -> GoalContract:
        """读取最新 GoalContract；无参数，返回契约，不存在时抛错。"""
        return self.contract_store.load_latest()

    def _next_participant(self, agent: AgentName) -> AgentName:
        """返回轮转中的下一参与者；参数为当前 Agent，返回下一个 Agent，不抛异常。"""
        index = self.config.participants.index(agent)
        return self.config.participants[(index + 1) % len(self.config.participants)]

    @staticmethod
    def _safe_token(value: str) -> str:
        """规范 invocation 片段；参数为文本，返回文件名安全标识。"""
        return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "node"

    def _call_agent(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        phase: str,
        turn_id: str,
        read_only: bool,
        format_attempt: int = 0,
        process_attempt: int = 0,
    ) -> ResponseModel:
        """幂等调用一个 Agent。

        参数为 Agent/协议/Prompt/阶段/回合/只读标记；返回协议模型；不确定调用拒绝重放。
        """
        self._ensure_components()
        assert self.sessions is not None
        workspace = self._ensure_workspace()
        session = self.sessions.get(agent)
        invocation_id = self._safe_token(
            f"INV-{phase}-{agent.value}-{turn_id}-G{session.generation}"
        )
        self._last_invocation_id = invocation_id
        prior = self.artifacts.find_invocation(invocation_id)
        if prior is not None:
            structured_ref = prior.get("structured_ref")
            if structured_ref:
                result = self.artifacts.read_agent_result(str(structured_ref))
                if result.structured_output is None:
                    raise ArtifactError("已保存 AgentResult 缺少结构化输出")
                if result.session_id:
                    self.sessions.bind(agent, result.session_id)
                return response_model.model_validate(result.structured_output)
            if prior.get("status") in {"prepared", "running"}:
                raise ArtifactError("调用状态不确定，拒绝盲目重放 Prompt")
        prompt_hash = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
        self.artifacts.prepare_invocation(
            invocation_id=invocation_id,
            agent=agent,
            turn_id=turn_id,
            base_commit=workspace.head(),
            goal_contract_version=self._contract().version,
            prompt_hash=prompt_hash,
        )
        before = workspace.snapshot().fingerprint
        try:
            result = self.backend.invoke(
                agent=agent,
                response_model=response_model,
                schema_name=schema_name,
                prompt=prompt,
                worktree=workspace.worktree_path,
                session_id=session.session_id,
                session_generation=session.generation,
                invocation_id=invocation_id,
                turn_id=turn_id,
                read_only=read_only,
            )
        except AdapterOutputError as exc:
            unchanged = workspace.snapshot().fingerprint == before
            if unchanged:
                self.artifacts.mark_invocation_failed(
                    invocation_id,
                    error=str(exc),
                )
            else:
                self.artifacts.mark_invocation_uncertain(
                    invocation_id,
                    error=f"{exc} after workspace changes",
                )
            if unchanged and format_attempt < self.config.format_retries:
                self.sessions.rebuild(agent, health=SessionHealth.UNCERTAIN)
                retry_prompt = self._rebuild_prompt(
                    agent,
                    (
                        f"{prompt}\n\n上一次响应格式无效。不得修改文件或执行新副作用；"
                        "仅重新返回当前任务要求的 JSON Schema 结果。"
                    ),
                )
                return self._call_agent(
                    agent=agent,
                    response_model=response_model,
                    schema_name=schema_name,
                    prompt=retry_prompt,
                    phase=f"{phase}-format-{format_attempt + 1}",
                    turn_id=turn_id,
                    read_only=read_only,
                    format_attempt=format_attempt + 1,
                    process_attempt=process_attempt,
                )
            raise exc
        except SessionBoundaryError:
            self.artifacts.mark_invocation_failed(
                invocation_id, error="resume sandbox boundary unverified"
            )
            session = self.sessions.rebuild(agent, health=SessionHealth.UNCERTAIN)
            return self._call_agent(
                agent=agent,
                response_model=response_model,
                schema_name=schema_name,
                prompt=self._rebuild_prompt(agent, prompt),
                phase=f"{phase}-rebuilt",
                turn_id=turn_id,
                read_only=read_only,
                format_attempt=format_attempt,
                process_attempt=process_attempt,
            )
        result_ref = self.artifacts.write_agent_result(invocation_id, result)
        self.artifacts.mark_invocation_result(
            invocation_id,
            result_ref=result.raw_output_ref,
            ok=result.ok,
            structured_ref=result_ref,
        )
        if not result.ok or result.structured_output is None or not result.session_id:
            unchanged = workspace.snapshot().fingerprint == before
            next_attempt = process_attempt + 1
            try:
                category = FailureCategory(
                    result.failure_category
                    or self.failure_classifier.classify(result.error or "").value
                )
            except ValueError:
                category = FailureCategory.PROCESS_ERROR
            identity = self._cli_identity(agent)
            if identity is not None and category in {
                FailureCategory.STARTUP_TIMEOUT,
                FailureCategory.IDLE_TIMEOUT,
                FailureCategory.NETWORK_ERROR,
                FailureCategory.CACHE_INCOMPATIBLE,
                FailureCategory.PROCESS_ERROR,
            }:
                self.circuit_breaker.record_failure(
                    identity,
                    category=category,
                    signature=f"runtime:{category.value}:{result.return_code}",
                )
            if (
                unchanged
                and self._is_retryable_failure(category)
                and next_attempt < self.config.max_process_failures
            ):
                if isinstance(self.backend, CLIBackend):
                    time.sleep(min(float(2**process_attempt), 4.0))
                self.sessions.rebuild(agent, health=SessionHealth.UNCERTAIN)
                return self._call_agent(
                    agent=agent,
                    response_model=response_model,
                    schema_name=schema_name,
                    prompt=self._rebuild_prompt(agent, prompt),
                    phase=f"{phase}-process-{next_attempt}",
                    turn_id=turn_id,
                    read_only=read_only,
                    format_attempt=format_attempt,
                    process_attempt=next_attempt,
                )
            raise RuntimeError(result.error or "Agent 未返回可恢复结构化结果")
        if read_only and workspace.snapshot().fingerprint != before:
            raise WorkspaceRaceError(f"{phase} 只读调用产生未授权写入")
        self.sessions.bind(agent, result.session_id)
        identity = self._cli_identity(agent)
        if identity is not None:
            self.circuit_breaker.record_success(identity)
        parsed = response_model.model_validate(result.structured_output)
        if getattr(parsed, "agent", agent) is not agent:
            raise RuntimeError("Agent 结构化输出身份不一致")
        return parsed

    def _rebuild_prompt(self, agent: AgentName, original_prompt: str) -> str:
        """为受控新 Session 注入权威上下文；参数为 Agent/原任务，返回组合 Prompt。"""
        self._ensure_components()
        assert self.memory is not None
        bootstrap = build_session_bootstrap_prompt(
            goal_contract=self._contract().model_dump_json(indent=2),
            current_head=self._ensure_workspace().head(),
            shared_summaries=self.memory.load_change_summaries(),
            issue_summary=self._ledger_attention(),
            validation_summary="请读取运行期 validation 报告并以其为权威结果",
        )
        return (
            f"{bootstrap}\n\n你是 {agent.value}。原生 Session 续接边界未经证明，"
            "本次使用受控新 Session；先自主核对当前 Git/workspace，"
            "再执行下列原任务：\n\n"
            f"{original_prompt}"
        )

    def _ack_prompt(
        self,
        agent: AgentName,
        *,
        update: PeerMemoryUpdate | None = None,
    ) -> str:
        """生成只读 Ack Prompt；参数为 Agent/可选更新，返回文本。"""
        workspace = self._ensure_workspace()
        contract = self._contract()
        if update is not None:
            prefix = build_peer_memory_prompt(update, current_head=workspace.head())
        else:
            prefix = build_session_bootstrap_prompt(
                goal_contract=contract.model_dump_json(indent=2),
                current_head=workspace.head(),
                shared_summaries=(
                    self.memory.load_change_summaries() if self.memory else []
                ),
                issue_summary=self._ledger_attention(),
                validation_summary="读取运行期 validation 报告",
            )
        return (
            f"{prefix}\n\n你是 {agent.value}。本调用严格只读，"
            "不得修改任何文件或 Git 状态。请自主读取当前 Git/workspace，"
            "并返回 ChangeAck JSON；commit_sha 必须等于当前 HEAD。"
        )

    def _sync_agent(self, agent: AgentName) -> None:
        """同步目标 Agent 的更新；参数为 Agent，无返回值，协议失败时抛错。"""
        self._ensure_components()
        assert self.memory is not None and self.sessions is not None
        workspace = self._ensure_workspace()
        pending = self.memory.pending(agent)
        if not pending and self.sessions.get(agent).last_ack_commit == workspace.head():
            return
        if not pending:
            ack = self._call_agent(
                agent=agent,
                response_model=ChangeAck,
                schema_name="change-ack.schema.json",
                prompt=self._ack_prompt(agent),
                phase="ack-head",
                turn_id=f"ACK-{workspace.head()[:12]}",
                read_only=True,
            )
            if ack.commit_sha != workspace.head():
                raise MemoryProtocolError("ChangeAck 未绑定当前 HEAD")
            self.sessions.record_ack(ack, update_id=f"HEAD-{workspace.head()}")
            return
        for update_id in pending:
            update = self.memory.get_update(update_id)
            ack = self._call_agent(
                agent=agent,
                response_model=ChangeAck,
                schema_name="change-ack.schema.json",
                prompt=self._ack_prompt(agent, update=update),
                phase=f"sync-{update_id}",
                turn_id=f"ACK-{update.turn_id}",
                read_only=True,
            )
            self.memory.acknowledge(
                update_id=update_id, ack=ack, current_head=workspace.head()
            )
            self.sessions.record_ack(ack, update_id=update_id)

    def _confirm_contract(self, agent: AgentName, *, version: int) -> None:
        """要求 Agent 显式读取新契约；参数为 Agent/版本，无返回值，绑定失败时抛错。"""
        self._ensure_components()
        assert self.sessions is not None
        workspace = self._ensure_workspace()
        ack = self._call_agent(
            agent=agent,
            response_model=ChangeAck,
            schema_name="change-ack.schema.json",
            prompt=(
                f"GoalContract 已更新为 v{version}。\n{self._ack_prompt(agent)}\n"
                f"请在理解 v{version} 后确认当前 HEAD。"
            ),
            phase=f"contract-v{version}",
            turn_id=f"ACK-CONTRACT-v{version}",
            read_only=True,
        )
        if ack.commit_sha != workspace.head():
            raise MemoryProtocolError("新 GoalContract ChangeAck 未绑定当前 HEAD")
        self.sessions.record_ack(ack, update_id=f"GOAL-CONTRACT-v{version}")
        self.contract_store.confirm(agent, version=version)

    def _write_policy(self) -> WritePolicy:
        """构建当前契约写策略；无参数，返回 WritePolicy，契约缺失时抛错。"""
        contract = self._contract()
        return WritePolicy(
            worktree=self._ensure_workspace().worktree_path,
            allowed_scope=contract.effective_scope,
            protected_scope=contract.protected_scope,
            high_impact_scope=self.config.high_impact_scope,
        )

    def _record_writer_ledger_addresses(
        self,
        *,
        writer: AgentName,
        commit_sha: str,
        changed_paths: tuple[str, ...],
        checkpoint_validation_passed: bool,
    ) -> None:
        """登记 Writer 对 Ledger 的处理；由审查阶段实现覆盖。"""
        raise NotImplementedError

    def _ledger_attention(self) -> list[str]:
        """返回当前阻断 Ledger 摘要；由审查阶段实现覆盖。"""
        raise NotImplementedError
