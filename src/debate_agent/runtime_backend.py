"""把确定性 Supervisor 组件接入 LangGraph 与真实 CLI 生命周期。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from debate_agent.adapters import (
    AGENT_ENVIRONMENT_PASSTHROUGH,
    ENVIRONMENT_OVERRIDE_ALLOWLIST,
    SENSITIVE_AGENT_ENVIRONMENT_KEYS,
    SENSITIVE_ENVIRONMENT_OVERRIDE_KEYS,
    AdapterContext,
    AdapterOutputError,
    AgentAdapter,
    adapter_factory,
)
from debate_agent.artifacts import ArtifactStore
from debate_agent.cli_health import (
    FailureCategory,
    FailureClassifier,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.invocation_events import (
    InvocationEventStore,
    InvocationEventType,
    InvocationSeverity,
    Redactor,
)
from debate_agent.models import (
    AgentName,
    AgentResult,
    FacilitatorContext,
    FacilitatorRecord,
)
from debate_agent.openclaw_recovery import (
    OpenClawSessionOutcome,
    inspect_openclaw_session,
)
from debate_agent.policy import CommandPolicy, PolicyViolationError
from debate_agent.process import (
    ProcessOutput,
    ProcessResult,
    ProcessRunner,
    ProcessSnapshot,
    ProcessTermination,
)
from debate_agent.protocol_gateway import ProtocolBinding, ProtocolGateway
from debate_agent.protocol_normalizer import DeepSeekProtocolNormalizer

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
BackendFactory = Callable[[RunConfig, ArtifactStore], "AgentBackend"]
ConfigPreflight = Callable[[RunConfig], RunConfig]
ResumeConfigPreflight = Callable[[RunConfig], RunConfig]


class FacilitatorBackend(Protocol):
    """声明 runtime 使用的 Facilitator 最小接口。"""

    def evaluate(self, context: FacilitatorContext) -> FacilitatorRecord:
        """评估上下文；参数为结构化事实，返回决定或 fallback 记录。"""
        raise RuntimeError("FacilitatorBackend 未实现 evaluate")


FacilitatorFactory = Callable[[RunConfig, ArtifactStore], FacilitatorBackend]

_NODE_PHASES = {
    "preflight": "preflight",
    "acquire_repo_lock": "preflight",
    "create_worktree": "environment",
    "prepare_environment": "environment",
    "sync_peer_memory": "debate",
    "grant_write_lease": "debate",
    "writer_turn": "debate",
    "quiesce_workspace": "debate",
    "enforce_policy": "debate",
    "run_checkpoint_validation": "validation",
    "commit_checkpoint": "debate",
    "publish_handoff": "handoff",
    "review_checkpoint": "debate",
    "update_goal_contract": "debate",
    "facilitator_assess": "facilitator",
    "progress_gate": "debate",
    "run_final_validation": "final_validation",
    "collect_blind_ballots": "completion",
    "generate_audit_bundle": "audit",
    "facilitator_audit_dispute": "facilitator",
    "materialize_final_tree": "audit",
    "collect_tree_acks": "completion",
    "create_integration_commit": "integration",
    "integrate_target": "integration",
    "preserve_result": "integration",
    "verify_target_tree": "integration",
    "cleanup_success": "completed",
    "recover_run": "recovery",
    "finalize_non_success": "stopped",
}

_NODE_LABELS = {
    "preflight": "检查工作空间",
    "acquire_repo_lock": "获取仓库写锁",
    "create_worktree": "创建隔离工作树",
    "prepare_environment": "准备并验证基线环境",
    "sync_peer_memory": "同步对方变更",
    "grant_write_lease": "授予唯一写租约",
    "quiesce_workspace": "等待工作空间稳定",
    "enforce_policy": "检查修改范围",
    "run_checkpoint_validation": "执行检查点验证",
    "commit_checkpoint": "提交 Agent 检查点",
    "publish_handoff": "发布交接摘要",
    "review_checkpoint": "审查当前检查点",
    "update_goal_contract": "更新目标契约",
    "facilitator_assess": "Facilitator 评估下一步",
    "progress_gate": "判断是否继续讨论",
    "run_final_validation": "执行最终验证",
    "collect_blind_ballots": "收集参与者完成判断",
    "generate_audit_bundle": "生成精简审计",
    "facilitator_audit_dispute": "Facilitator 处理审计争议",
    "materialize_final_tree": "生成最终文件树",
    "collect_tree_acks": "收集参与者文件树确认",
    "create_integration_commit": "创建最终聚焦提交",
    "integrate_target": "集成目标分支",
    "preserve_result": "保留未合并结果分支",
    "verify_target_tree": "核对目标分支文件树",
    "cleanup_success": "清理成功运行现场",
    "recover_run": "恢复中断运行",
    "finalize_non_success": "保存未完成现场",
}


class AgentBackend:
    """声明 runtime 调用真实或测试 CLI 的统一接口。"""

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """调用 Agent；参数为协议与 Session 上下文，返回统一结果，失败时抛错。"""
        raise RuntimeError("AgentBackend 未实现 invoke")


def _schema_path(name: str) -> Path:
    """定位运行时 JSON Schema；参数为文件名，返回路径，不存在时抛错。"""
    package_path = Path(__file__).resolve().parent / "schemas" / name
    if package_path.is_file():
        return package_path
    source_path = Path(__file__).resolve().parents[2] / "schemas" / name
    if source_path.is_file():
        return source_path
    raise FileNotFoundError(f"协议 Schema 不存在: {name}")


class CLIBackend(AgentBackend):
    """使用注册的 CLI Adapter 调用本次选定的真实 Agent。"""

    def __init__(
        self,
        *,
        config: RunConfig,
        artifacts: ArtifactStore,
        command_policy: CommandPolicy,
        env: Mapping[str, str],
        remaining_run_seconds: Callable[[], float] | None = None,
        head_resolver: Callable[[Path], str] | None = None,
        goal_contract_version: Callable[[], int] | None = None,
        protocol_gateway: ProtocolGateway | None = None,
    ) -> None:
        """创建 CLI backend。

        参数为配置、运行产物、命令策略、环境和可选协议依赖；返回实例；不启动进程。
        """
        self.config = config
        self.artifacts = artifacts
        self.command_policy = command_policy
        self.env = dict(env)
        self.runner = ProcessRunner(raw_root=artifacts.run_dir / "raw")
        self.remaining_run_seconds = remaining_run_seconds
        self.head_resolver = head_resolver or self._git_head
        self.goal_contract_version = goal_contract_version or (lambda: 1)
        self.protocol_gateway = protocol_gateway or ProtocolGateway(
            normalizer=DeepSeekProtocolNormalizer(
                config=config,
                environment=os.environ,
            ),
            counterexample_ballot_enabled=config.counterexample_ballot,
        )
        self.failure_classifier = FailureClassifier()

    @staticmethod
    def _git_head(worktree: Path) -> str:
        """读取调用边界的 Git HEAD。

        参数为工作树；返回完整提交 SHA；目录不是有效 Git 工作树时抛运行错误。
        """
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = " ".join(result.stderr.split()) or "无法读取 HEAD"
            raise RuntimeError(detail)
        return result.stdout.strip()

    def _profile(self, agent: AgentName) -> AgentProfile:
        """解析 CLI 可执行文件；参数为 Agent，返回冻结 Profile，不可用时抛错。"""
        profile = self.config.profiles[agent]
        command = shutil.which(profile.command, path=self.env.get("PATH"))
        if command is None and Path(profile.command).is_file():
            command = str(Path(profile.command).resolve())
        if command is None:
            raise FileNotFoundError(f"CLI 不可用: {profile.command}")
        return profile.model_copy(update={"command": command})

    def _adapter(
        self, agent: AgentName, profile: AgentProfile, schema: Path
    ) -> AgentAdapter:
        """创建厂商 Adapter。

        参数为 Agent、Profile 和协议 Schema；返回对应 Adapter；未知 Agent 时抛错。
        """
        context = AdapterContext(
            schema_path=schema,
            mcp_mode=self.config.mcp_mode,
            unrestricted_commands=self.config.unrestricted_commands,
            codex_resume_sandbox_verified=(self.config.codex_resume_sandbox_verified),
            command_policy=self.command_policy,
        )
        return adapter_factory(agent)(profile, context)

    def _invocation_environment(
        self,
        agent: AgentName,
        overrides: tuple[tuple[str, str], ...],
    ) -> dict[str, str]:
        """合并单次调用环境。

        参数为受信任 Agent 和 Adapter 覆盖项；返回 self.env 的局部副本；未知 Agent、
        非法字符串、重复键或非静态允许键时抛 PolicyViolationError，且不修改 base env。
        """
        if not isinstance(agent, AgentName):
            raise PolicyViolationError(f"未知 Agent 环境覆盖: {agent}")
        try:
            allowed = ENVIRONMENT_OVERRIDE_ALLOWLIST[agent]
        except KeyError as exc:
            raise PolicyViolationError(f"未知 Agent 环境覆盖: {agent.value}") from exc
        merged = dict(self.env)
        seen: set[str] = set()
        for item in overrides:
            if not isinstance(item, tuple) or len(item) != 2:
                raise PolicyViolationError("环境覆盖必须是键值字符串对")
            name, value = item
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise PolicyViolationError("环境覆盖包含非法字符串")
            if name in seen:
                raise PolicyViolationError(f"环境覆盖键重复: {name}")
            seen.add(name)
            if name not in allowed:
                raise PolicyViolationError(f"Agent 不允许覆盖环境键: {name}")
            merged[name] = value
        return merged

    def _mark_started(self, invocation_id: str, pid: int, group: int) -> None:
        """记录 CLI 进程；参数为调用/PID/进程组，无返回值，落盘失败时抛错。"""
        self.artifacts.mark_invocation_running(
            invocation_id,
            pid=pid,
            process_group=group,
        )

    @staticmethod
    def _redact_agent_result(result: AgentResult, redactor: Redactor) -> AgentResult:
        """脱敏统一调用结果。

        参数为 AgentResult 和本次调用 Redactor；返回不含已知环境覆盖值的结果；
        仅递归处理公开字符串字段，不修改布尔、数字或 Session 运行边界。
        """

        def redact_value(value: Any) -> Any:
            """递归脱敏协议值；参数为任意 JSON 值；返回保持容器形态的安全副本。"""
            if isinstance(value, str):
                return redactor.redact(value)
            if isinstance(value, dict):
                return {key: redact_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact_value(item) for item in value]
            return value

        updates: dict[str, object] = {}
        if result.structured_output is not None:
            updates["structured_output"] = redact_value(result.structured_output)
        if result.error is not None:
            updates["error"] = redactor.redact(result.error)
        return result.model_copy(update=updates) if updates else result

    @staticmethod
    def _workspace_fingerprint(worktree: Path, env: Mapping[str, str]) -> str:
        """读取 Git 工作区指纹。

        参数为工作树和过滤环境；返回 status 摘要；非 Git 目录返回稳定占位符。
        """
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=dict(env),
        )
        return result.stdout if result.returncode == 0 else "workspace-unavailable"

    @staticmethod
    def _is_openclaw_ambiguous(process: ProcessResult) -> bool:
        """判断 OpenClaw 失败是否可能已被 Gateway 接收。

        参数：process 为首次调用结果。
        返回值：连接/分层超时或 Gateway 传输错误时为 True。
        异常：不抛出异常。
        """
        if process.termination in {
            ProcessTermination.STARTUP_TIMEOUT,
            ProcessTermination.IDLE_TIMEOUT,
            ProcessTermination.HARD_TIMEOUT,
        }:
            return True
        detail = f"{process.stderr}\n{process.stdout}".casefold()
        return any(
            marker in detail
            for marker in (
                "gateway timeout",
                "websocket",
                "connection reset",
                "connection closed",
                "connection refused",
            )
        )

    def _recover_openclaw_process(
        self,
        *,
        profile: AgentProfile,
        invocation_session_id: str,
        worktree: Path,
        process: ProcessResult,
    ) -> tuple[ProcessResult, FailureCategory | None]:
        """查询 OpenClaw Session 并决定是否允许重放。

        参数：profile 为冻结 CLI 配置，invocation_session_id 为规范 Key，
        worktree 为只读查询目录，process 为模糊失败。
        返回值：恢复后的进程结果及可选强制故障类别；只有明确未接收不强制阻断。
        异常：查询启动错误会安全降级为 ambiguous_transport。
        """
        native_profile = (
            profile.native_profile.strip() if profile.native_profile else "main"
        )
        command = (
            profile.command,
            "sessions",
            "--agent",
            native_profile,
            "--json",
            "--limit",
            "all",
        )
        home = Path(self.env.get("HOME", str(Path.home()))).expanduser()
        inspection = None
        for attempt in range(3):
            try:
                query = subprocess.run(
                    command,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=min(float(profile.startup_timeout_seconds), 30.0),
                    env=self.env,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return process, FailureCategory.AMBIGUOUS_TRANSPORT
            if query.returncode != 0:
                return process, FailureCategory.AMBIGUOUS_TRANSPORT
            inspection = inspect_openclaw_session(
                query.stdout,
                expected_session_key=invocation_session_id,
                allowed_sessions_root=(
                    home / ".openclaw" / "agents" / native_profile / "sessions"
                ),
            )
            if inspection.outcome is not OpenClawSessionOutcome.RUNNING or attempt == 2:
                break
            time.sleep(1.0)
        assert inspection is not None
        if inspection.outcome is OpenClawSessionOutcome.NOT_RECEIVED:
            return process, None
        if inspection.outcome is not OpenClawSessionOutcome.COMPLETED:
            return process, FailureCategory.AMBIGUOUS_TRANSPORT
        assert inspection.content is not None
        recovered_stdout = json.dumps(
            {
                "status": "ok",
                "result": {
                    "payloads": [{"text": inspection.content}],
                    "meta": {
                        "systemPromptReport": {
                            "sessionKey": invocation_session_id,
                        }
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            replace(
                process,
                return_code=0,
                stdout=recovered_stdout,
                stdout_truncated=False,
                timed_out=False,
                cancelled=False,
                termination=ProcessTermination.COMPLETED,
            ),
            None,
        )

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """调用真实 CLI。

        参数为 Agent/协议/Prompt/worktree/Session/调用身份；返回结果；
        进程或解析失败时抛错。
        """
        schema = _schema_path(schema_name)
        profile = self._profile(agent)
        adapter = self._adapter(agent, profile, schema)
        binding = ProtocolBinding(
            agent=agent,
            turn_id=turn_id,
            head_sha=self.head_resolver(worktree),
            goal_contract_version=self.goal_contract_version(),
        )
        invocation = adapter.build_invocation(
            worktree=worktree,
            prompt=prompt,
            session_id=session_id,
        )
        invocation_env = self._invocation_environment(
            agent, invocation.environment_overrides
        )
        agent_secret_values: list[str] = []
        for name in AGENT_ENVIRONMENT_PASSTHROUGH[agent]:
            value = os.environ.get(name)
            if not value:
                continue
            invocation_env[name] = value
            if name in SENSITIVE_AGENT_ENVIRONMENT_KEYS[agent]:
                agent_secret_values.append(value)
        hermes_secret_values: tuple[str, ...] = ()
        if (
            agent is AgentName.HERMES
            and profile.provider == "deepseek"
            and os.environ.get("DEEPSEEK_API_KEY")
        ):
            key = os.environ["DEEPSEEK_API_KEY"]
            invocation_env["DEEPSEEK_API_KEY"] = key
            hermes_secret_values = (key,)
        secret_values = (
            tuple(
                self.env[name]
                for name in self.config.proxy_environment_allowlist
                if name in self.env
            )
            + tuple(
                value
                for name, value in invocation.environment_overrides
                if name in SENSITIVE_ENVIRONMENT_OVERRIDE_KEYS[agent]
            )
            + tuple(agent_secret_values)
            + hermes_secret_values
        )
        redactor = Redactor(
            workspace=worktree,
            secret_values=secret_values,
            session_ids=(session_id,) if session_id else (),
        )
        event_store = InvocationEventStore(
            state_root=self.config.state_root,
            run_id=self.artifacts.run_id,
            redactor=redactor,
        )
        parser = adapter.create_stream_parser(workspace=worktree)
        activity_by_output: dict[int, bool] = {}
        last_useful_elapsed = 0.0

        def publish_output(output: ProcessOutput) -> None:
            """发布厂商增量事件。

            参数为解码输出；无返回值；解析或事件持久化错误会传播并终止调用。
            """
            nonlocal last_useful_elapsed
            drafts = parser.feed(output)
            useful = any(draft.useful for draft in drafts)
            activity_by_output[id(output)] = useful
            for draft in drafts:
                idle = (
                    0.0
                    if draft.useful
                    else max(output.elapsed_seconds - last_useful_elapsed, 0.0)
                )
                if draft.useful:
                    last_useful_elapsed = output.elapsed_seconds
                if self.config.realtime_output_enabled:
                    event_store.publish(
                        invocation_id=invocation_id,
                        agent=agent,
                        turn_id=turn_id,
                        event_type=draft.event_type,
                        title=draft.title,
                        summary=draft.summary,
                        severity=draft.severity,
                        elapsed_seconds=output.elapsed_seconds,
                        idle_seconds=idle,
                        changed_paths=draft.changed_paths,
                        useful=draft.useful,
                        terminal=draft.terminal,
                    )

        def useful_activity(output: ProcessOutput) -> bool:
            """读取解析结果的活动标记；参数为同一输出，返回是否有效活动。"""
            return activity_by_output.pop(id(output), False)

        def publish_heartbeat(snapshot: ProcessSnapshot) -> None:
            """发布调用心跳；参数为进程快照，无返回值，事件写入失败时抛错。"""
            if not self.config.realtime_output_enabled:
                return
            event_store.publish(
                invocation_id=invocation_id,
                agent=agent,
                turn_id=turn_id,
                event_type=InvocationEventType.HEARTBEAT,
                title=f"{agent.value.capitalize()} CLI 仍在运行",
                elapsed_seconds=snapshot.elapsed_seconds,
                idle_seconds=snapshot.idle_seconds,
                useful=False,
            )

        def mark_started(pid: int, group: int) -> None:
            """记录进程并发布启动事件；参数为 PID/进程组，无返回值，落盘失败时抛错。"""
            self._mark_started(invocation_id, pid, group)
            if self.config.realtime_output_enabled:
                event_store.publish(
                    invocation_id=invocation_id,
                    agent=agent,
                    turn_id=turn_id,
                    event_type=InvocationEventType.STARTED,
                    title=f"{agent.value.capitalize()} CLI 已启动",
                    elapsed_seconds=0,
                    idle_seconds=0,
                    useful=False,
                )

        hard_timeout = float(invocation.hard_timeout_seconds)
        if self.remaining_run_seconds is not None:
            remaining = self.remaining_run_seconds()
            if remaining <= 0:
                raise RuntimeError("达到累计活跃运行时限")
            hard_timeout = min(hard_timeout, remaining)
        process = self.runner.run(
            invocation_id=invocation_id,
            argv=invocation.argv,
            audit_argv=invocation.audit_argv,
            temporary_inputs=invocation.temporary_inputs,
            cwd=invocation.cwd,
            stdin=invocation.stdin,
            timeout_seconds=hard_timeout,
            env=invocation_env,
            on_started=mark_started,
            cancel_requested=self.artifacts.abort_requested,
            startup_timeout_seconds=min(
                float(invocation.startup_timeout_seconds), hard_timeout
            ),
            idle_timeout_seconds=min(
                float(invocation.idle_timeout_seconds), hard_timeout
            ),
            hard_timeout_seconds=hard_timeout,
            heartbeat_interval_seconds=min(
                float(invocation.heartbeat_interval_seconds), hard_timeout
            ),
            on_output=publish_output,
            is_useful_activity=useful_activity,
            on_heartbeat=publish_heartbeat,
            workspace_fingerprint=lambda: self._workspace_fingerprint(
                worktree, invocation_env
            ),
            stdout_persist_transform=invocation.stdout_persist_transform,
        )
        failure_category_override: FailureCategory | None = None
        if (
            agent is AgentName.OPENCLAW
            and invocation.session_id is not None
            and self._is_openclaw_ambiguous(process)
        ):
            process, failure_category_override = self._recover_openclaw_process(
                profile=profile,
                invocation_session_id=invocation.session_id,
                worktree=worktree,
                process=process,
            )
        try:
            result = self.protocol_gateway.parse_result(
                adapter=adapter,
                result=process,
                response_model=response_model,
                binding=binding,
                session_generation=session_generation,
                normalizer_text=redactor.redact,
            )
            result = self._redact_agent_result(result, redactor)
        except AdapterOutputError:
            if self.config.realtime_output_enabled:
                event_store.publish(
                    invocation_id=invocation_id,
                    agent=agent,
                    turn_id=turn_id,
                    event_type=InvocationEventType.TERMINATED,
                    title=f"{agent.value.capitalize()} 协议失败",
                    severity=InvocationSeverity.ERROR,
                    elapsed_seconds=process.elapsed_seconds,
                    idle_seconds=max(
                        process.elapsed_seconds - last_useful_elapsed, 0.0
                    ),
                    useful=True,
                    terminal=True,
                    termination_category=FailureCategory.PROTOCOL_ERROR.value,
                )
            raise
        category = (
            None
            if result.ok
            else failure_category_override
            or self.failure_classifier.classify(
                f"{process.stderr}\n{result.error or ''}",
                termination=process.termination,
            )
        )
        if (
            not result.ok
            and category is FailureCategory.AMBIGUOUS_TRANSPORT
            and invocation.session_id is not None
        ):
            result = result.model_copy(update={"session_id": invocation.session_id})
        if self.config.realtime_output_enabled:
            event_store.publish(
                invocation_id=invocation_id,
                agent=agent,
                turn_id=turn_id,
                event_type=InvocationEventType.TERMINATED,
                title=(
                    f"{agent.value.capitalize()} CLI 已完成"
                    if result.ok
                    else f"{agent.value.capitalize()} CLI 失败"
                ),
                summary=category.value if category is not None else "",
                severity=(
                    InvocationSeverity.INFO if result.ok else InvocationSeverity.ERROR
                ),
                elapsed_seconds=process.elapsed_seconds,
                idle_seconds=max(process.elapsed_seconds - last_useful_elapsed, 0.0),
                useful=True,
                terminal=True,
                termination_category=category.value if category is not None else None,
            )
        return (
            result.model_copy(update={"failure_category": category.value})
            if category is not None
            else result
        )
