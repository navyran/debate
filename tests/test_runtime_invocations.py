from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from debate_agent.artifacts import ArtifactStore
from debate_agent.cli_health import FailureCategory
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.invocation_events import (
    InvocationEventStore,
    InvocationEventType,
    Redactor,
)
from debate_agent.models import AgentName, AgentResult, TurnResult
from debate_agent.policy import CommandPolicy, PolicyViolationError
from debate_agent.process import ProcessOutput, ProcessResult
from debate_agent.runtime import CLIBackend, DebateLifecycleService, DebateRuntime


def turn_payload() -> dict[str, object]:
    """构造 Codex 终局协议；无参数，返回协议字典，不抛异常。"""
    return {
        "agent": "codex",
        "turn_id": "TURN-1",
        "base_commit": "abc",
        "observed_head": "abc",
        "goal_contract_version": 1,
        "decision": "accept",
        "summary": "done",
        "accepted_points": [],
        "issues": [],
        "requested_scope_additions": [],
        "requested_validation_additions": [],
        "completion_assessment": "complete",
    }


def make_fake_codex(
    path: Path,
    *,
    delay_seconds: float,
    fail_with_tls: bool = False,
) -> Path:
    """创建可执行 fake Codex。

    参数为路径、延迟和失败开关；返回脚本路径；写入或 chmod 失败时抛 OSError。
    """
    payload = json.dumps(turn_payload(), ensure_ascii=False)
    thread_line = json.dumps(
        {"type": "thread.started", "thread_id": "session-secret-123"}
    )
    command_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "pytest -q"},
        }
    )
    message_line = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": payload},
        }
    )
    turn_line = json.dumps({"type": "turn.completed"})
    source = (
        f"#!{sys.executable}\n"
        "import sys, time\n"
        + (
            "print('WebSocket TLS handshake failed', file=sys.stderr, flush=True)\n"
            "raise SystemExit(1)\n"
            if fail_with_tls
            else (
                f"print({thread_line!r}, flush=True)\n"
                f"print({command_line!r}, flush=True)\n"
                f"time.sleep({delay_seconds!r})\n"
                f"print({message_line!r}, flush=True)\n"
                f"print({turn_line!r}, flush=True)\n"
            )
        )
    )
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def build_backend(
    tmp_path: Path,
    *,
    command: Path,
    hard_timeout_seconds: int = 2,
    remaining_run_seconds=None,
) -> tuple[CLIBackend, ArtifactStore]:
    """创建真实 CLIBackend 测试夹具。

    参数为目录、命令、硬上限和剩余预算回调；返回 Backend/Store；创建失败时抛错。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        profiles={
            AgentName.CODEX: AgentProfile(
                command=str(command),
                model="gpt-test",
                version="0.144.5",
                timeout_seconds=hard_timeout_seconds,
                startup_timeout_seconds=1,
                idle_timeout_seconds=1,
                hard_timeout_seconds=hard_timeout_seconds,
                heartbeat_interval_seconds=1,
            ),
            AgentName.CLAUDE: AgentProfile(
                command=str(command), model="sonnet", version="2.1.211"
            ),
        },
    )
    store = ArtifactStore.create(config, run_id="RUN-LIVE")
    backend = CLIBackend(
        config=config,
        artifacts=store,
        command_policy=CommandPolicy(unrestricted_commands=True),
        env={"PATH": os.environ.get("PATH", "")},
        remaining_run_seconds=remaining_run_seconds,
        head_resolver=lambda _: "abc",
        goal_contract_version=lambda: 1,
    )
    return backend, store


def prepare_invocation(store: ArtifactStore, invocation_id: str) -> None:
    """创建调用 journal；参数为 Store/ID，无返回值，冲突时抛错。"""
    store.prepare_invocation(
        invocation_id=invocation_id,
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit="abc",
        goal_contract_version=1,
        prompt_hash="sha256:test",
    )


def test_cli_backend_publishes_events_before_codex_process_finishes(
    tmp_path: Path,
) -> None:
    command = make_fake_codex(tmp_path / "fake-codex", delay_seconds=0.25)
    backend, store = build_backend(tmp_path, command=command)
    prepare_invocation(store, "INV-LIVE")
    holder: dict[str, object] = {}

    def invoke() -> None:
        """在线程调用 Backend；无参数和返回值，结果写入 holder。"""
        holder["result"] = backend.invoke(
            agent=AgentName.CODEX,
            response_model=TurnResult,
            schema_name="turn.schema.json",
            prompt="secret prompt",
            worktree=tmp_path,
            session_id=None,
            session_generation=1,
            invocation_id="INV-LIVE",
            turn_id="TURN-1",
            read_only=False,
        )

    thread = threading.Thread(target=invoke)
    thread.start()
    event_store = InvocationEventStore(state_root=tmp_path / "state", run_id="RUN-LIVE")
    deadline = time.monotonic() + 1
    events = []
    while time.monotonic() < deadline and thread.is_alive():
        events = event_store.read_events()
        if any(
            event.event_type is InvocationEventType.TOOL_STARTED
            for event in events
        ):
            break
        time.sleep(0.01)

    assert thread.is_alive(), "实时事件必须在 CLI 完成前落盘"
    assert any(event.event_type is InvocationEventType.TOOL_STARTED for event in events)
    assert "session-secret-123" not in event_store.events_path.read_text()
    thread.join(timeout=2)
    assert not thread.is_alive()
    result = holder["result"]
    assert result.ok is True
    final_events = event_store.read_events()
    assert final_events[-1].terminal is True


def test_cli_backend_classifies_network_error_and_applies_remaining_run_budget(
    tmp_path: Path,
) -> None:
    failing = make_fake_codex(
        tmp_path / "fake-codex-fail",
        delay_seconds=0,
        fail_with_tls=True,
    )
    backend, store = build_backend(tmp_path, command=failing)
    prepare_invocation(store, "INV-NETWORK")

    network = backend.invoke(
        agent=AgentName.CODEX,
        response_model=TurnResult,
        schema_name="turn.schema.json",
        prompt="prompt",
        worktree=tmp_path,
        session_id=None,
        session_generation=1,
        invocation_id="INV-NETWORK",
        turn_id="TURN-1",
        read_only=True,
    )

    assert network.ok is False
    assert network.failure_category == FailureCategory.NETWORK_ERROR.value
    network_progress = InvocationEventStore(
        state_root=tmp_path / "state", run_id="RUN-LIVE"
    ).read_progress()
    assert network_progress is not None
    assert network_progress.agents[AgentName.CODEX].termination_category == (
        FailureCategory.NETWORK_ERROR.value
    )

    active = make_fake_codex(tmp_path / "fake-codex-active", delay_seconds=0.3)
    budget_backend, budget_store = build_backend(
        tmp_path / "budget",
        command=active,
        hard_timeout_seconds=2,
        remaining_run_seconds=lambda: 0.08,
    )
    prepare_invocation(budget_store, "INV-BUDGET")
    bounded = budget_backend.invoke(
        agent=AgentName.CODEX,
        response_model=TurnResult,
        schema_name="turn.schema.json",
        prompt="prompt",
        worktree=tmp_path / "budget",
        session_id=None,
        session_generation=1,
        invocation_id="INV-BUDGET",
        turn_id="TURN-1",
        read_only=False,
    )

    assert bounded.ok is False
    assert bounded.failure_category == FailureCategory.HARD_TIMEOUT.value
    assert bounded.elapsed_seconds < 0.3


def test_lifecycle_runs_health_preflight_before_artifact_or_repo_lock(
    tmp_path: Path,
) -> None:
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
    )
    calls = []

    def block(run_config: RunConfig) -> RunConfig:
        """模拟健康预检失败；参数为配置，始终抛 RuntimeError。"""
        calls.append(run_config)
        raise RuntimeError("CLI health blocked")

    service = DebateLifecycleService(config_preflight=block)

    with pytest.raises(RuntimeError, match="health blocked"):
        service.run_with_id(config, run_id="RUN-PREFLIGHT")

    assert calls == [config]
    assert not (config.state_root / "runs" / "RUN-PREFLIGHT").exists()
    assert not list(config.state_root.glob("locks/**/*"))


def test_runtime_retries_only_retryable_infrastructure_categories() -> None:
    assert DebateRuntime._is_retryable_failure(FailureCategory.NETWORK_ERROR)
    assert DebateRuntime._is_retryable_failure(FailureCategory.STARTUP_TIMEOUT)
    assert DebateRuntime._is_retryable_failure(FailureCategory.IDLE_TIMEOUT)
    assert DebateRuntime._is_retryable_failure(FailureCategory.PROCESS_ERROR)
    assert not DebateRuntime._is_retryable_failure(
        FailureCategory.AMBIGUOUS_TRANSPORT
    )
    assert not DebateRuntime._is_retryable_failure(FailureCategory.HARD_TIMEOUT)
    assert not DebateRuntime._is_retryable_failure(FailureCategory.CACHE_INCOMPATIBLE)
    assert not DebateRuntime._is_retryable_failure(FailureCategory.VALIDATION_ERROR)
    assert not DebateRuntime._is_retryable_failure(FailureCategory.POLICY_VIOLATION)


class RecordingProcessRunner:
    """记录 Runtime 传给 ProcessRunner 的调用环境并返回 OpenCode 成功结果。"""

    def __init__(self, payload: dict[str, object]) -> None:
        """创建记录器；参数为终局协议；返回实例；不执行子进程。"""
        self.payload = payload
        self.environment: dict[str, str] | None = None
        self.fingerprint = ""

    def run(self, **kwargs: Any) -> ProcessResult:
        """模拟进程执行；参数为 Runtime 调用字段；返回成功结果；回调错误会传播。"""
        environment = dict(cast(Mapping[str, str], kwargs["env"]))
        self.environment = environment
        fingerprint = cast(Any, kwargs["workspace_fingerprint"])
        self.fingerprint = fingerprint()
        on_output = cast(Any, kwargs["on_output"])
        config_content = environment["OPENCODE_CONFIG_CONTENT"]
        on_output(
            ProcessOutput(
                stream="stdout",
                text=json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session-1",
                        "text": config_content,
                    }
                )
                + "\n",
                elapsed_seconds=0.1,
                complete_line=True,
            )
        )
        on_output(
            ProcessOutput(
                stream="stderr",
                text=config_content + "\n",
                elapsed_seconds=0.1,
                complete_line=True,
            )
        )
        terminal_payload = {**self.payload, "summary": config_content}
        stdout = json.dumps(
            {
                "type": "text",
                "sessionID": "session-1",
                "text": json.dumps(terminal_payload),
            }
        )
        return ProcessResult(
            return_code=0,
            stdout=stdout,
            stderr=config_content,
            elapsed_seconds=0.2,
            raw_output_ref="raw/INV-OPEN.json",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            cancelled=False,
        )


def build_opencode_backend(
    tmp_path: Path,
) -> tuple[CLIBackend, ArtifactStore, dict[str, str]]:
    """创建 OpenCode Backend；参数为临时目录；返回 Backend/Store/base env。"""
    command = tmp_path / "opencode"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.OPENCODE: AgentProfile(
                command=str(command), model="provider/model"
            ),
        },
    )
    store = ArtifactStore.create(config, run_id="RUN-OPEN")
    base_env = {"PATH": os.environ.get("PATH", ""), "BASE_ONLY": "base-value"}
    backend = CLIBackend(
        config=config,
        artifacts=store,
        command_policy=CommandPolicy(unrestricted_commands=True),
        env=base_env,
        head_resolver=lambda _: "abc",
        goal_contract_version=lambda: 1,
    )
    return backend, store, base_env


def test_cli_backend_merges_opencode_environment_for_process_and_fingerprint(
    tmp_path: Path,
) -> None:
    backend, store, base_env = build_opencode_backend(tmp_path)
    prepare_invocation(store, "INV-OPEN")
    payload = {
        **turn_payload(),
        "agent": "opencode",
        "accepted_points": ["ordinary true remains"],
    }
    runner = RecordingProcessRunner(payload)
    backend.runner = cast(Any, runner)
    captured_fingerprint_env: dict[str, str] = {}

    def fingerprint(_worktree: Path, env: Mapping[str, str]) -> str:
        """记录指纹环境；参数为工作树/环境；返回固定指纹。"""
        captured_fingerprint_env.update(env)
        return "clean"

    backend._workspace_fingerprint = fingerprint  # type: ignore[method-assign]

    parsed = backend.invoke(
        agent=AgentName.OPENCODE,
        response_model=TurnResult,
        schema_name="turn.schema.json",
        prompt="private prompt",
        worktree=tmp_path,
        session_id=None,
        session_generation=1,
        invocation_id="INV-OPEN",
        turn_id="TURN-1",
        read_only=False,
    )

    assert parsed.ok is True
    assert runner.environment is not None
    assert runner.environment["BASE_ONLY"] == "base-value"
    assert set(runner.environment) == {
        *base_env,
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
    }
    assert captured_fingerprint_env == runner.environment
    assert backend.env == base_env
    config_content = runner.environment["OPENCODE_CONFIG_CONTENT"]
    assert parsed.structured_output is not None
    assert parsed.structured_output["summary"] != config_content
    assert parsed.structured_output["accepted_points"] == [
        "ordinary true remains"
    ]
    assert config_content not in parsed.model_dump_json()
    public_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in backend.artifacts.run_dir.rglob("*")
        if path.is_file() and "raw" not in path.parts
    )
    assert config_content not in public_artifacts
    assert "<redacted>" in public_artifacts


def test_cli_backend_scopes_and_redacts_codebuddy_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 CodeBuddy 凭据只进入其调用环境且不会进入公开结果。"""
    command = tmp_path / "codebuddy"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    secret = "codebuddy-secret-value"
    monkeypatch.setenv("CODEBUDDY_API_KEY", secret)
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state-codebuddy",
        participants=(AgentName.CODEX, AgentName.CODEBUDDY),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.CODEBUDDY: AgentProfile(
                command=str(command),
                model="codebuddy-model",
                version="1.0.0",
                runtime_config_hash="a" * 64,
            ),
        },
    )
    store = ArtifactStore.create(config, run_id="RUN-CODEBUDDY")
    backend = CLIBackend(
        config=config,
        artifacts=store,
        command_policy=CommandPolicy(unrestricted_commands=True),
        env={"PATH": os.environ.get("PATH", ""), "BASE_ONLY": "base-value"},
        head_resolver=lambda _: "abc",
        goal_contract_version=lambda: 1,
    )
    prepare_invocation(store, "INV-CODEBUDDY")

    class CodeBuddyRunner:
        """记录 CodeBuddy 环境并返回含凭据的模拟终局。"""

        environment: dict[str, str] | None = None

        def run(self, **kwargs: Any) -> ProcessResult:
            """模拟 CodeBuddy；参数为调用字段，返回成功结果。"""
            self.environment = dict(cast(Mapping[str, str], kwargs["env"]))
            payload = {
                **turn_payload(),
                "agent": "codebuddy",
                "summary": secret,
            }
            return ProcessResult(
                return_code=0,
                stdout=json.dumps(
                    {
                        "session_id": "codebuddy-session",
                        "structured_output": payload,
                    }
                ),
                stderr="",
                elapsed_seconds=0.1,
                raw_output_ref="raw/INV-CODEBUDDY.json",
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
                cancelled=False,
            )

    runner = CodeBuddyRunner()
    backend.runner = cast(Any, runner)
    parsed = backend.invoke(
        agent=AgentName.CODEBUDDY,
        response_model=TurnResult,
        schema_name="turn.schema.json",
        prompt="private prompt",
        worktree=tmp_path,
        session_id=None,
        session_generation=1,
        invocation_id="INV-CODEBUDDY",
        turn_id="TURN-1",
        read_only=False,
    )

    assert parsed.ok is True
    assert runner.environment is not None
    assert runner.environment["CODEBUDDY_API_KEY"] == secret
    assert backend.env == {
        "PATH": os.environ.get("PATH", ""),
        "BASE_ONLY": "base-value",
    }
    assert parsed.structured_output is not None
    assert parsed.structured_output["summary"] == "<redacted>"
    public_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in store.run_dir.rglob("*")
        if path.is_file() and "raw" not in path.parts
    )
    assert secret not in public_artifacts


def test_agent_result_redaction_preserves_json_boolean_and_plain_true() -> None:
    """验证结果脱敏保留布尔和普通 true；无参数，无返回值。"""
    config_content = '{"permission":"allow","nonce":"high-entropy-0123456789"}'
    result = AgentResult(
        ok=True,
        agent=AgentName.OPENCODE,
        session_generation=1,
        turn_id="TURN-1",
        structured_output={
            "enabled": True,
            "message": "ordinary true remains",
            "config": config_content,
        },
        elapsed_seconds=0.1,
        raw_output_ref="raw/INV-OPEN.json",
    )

    redacted = CLIBackend._redact_agent_result(
        result,
        Redactor(workspace=None, secret_values=(config_content,)),
    )

    assert redacted.structured_output == {
        "enabled": True,
        "message": "ordinary true remains",
        "config": "<redacted>",
    }


def test_cli_backend_rejects_unknown_duplicate_invalid_or_unapproved_overrides(
    tmp_path: Path,
) -> None:
    backend, _store, base_env = build_opencode_backend(tmp_path)
    secret = "must-not-leak"

    invalid_cases = (
        (
            AgentName.CODEX,
            (("OPENCODE_CONFIG_CONTENT", secret),),
        ),
        (
            AgentName.OPENCODE,
            (
                ("OPENCODE_CONFIG_CONTENT", "one"),
                ("OPENCODE_CONFIG_CONTENT", secret),
            ),
        ),
        (
            AgentName.OPENCODE,
            cast(tuple[tuple[str, str], ...], ((1, secret),)),
        ),
        (
            cast(AgentName, "unknown"),
            (),
        ),
    )
    for agent, overrides in invalid_cases:
        with pytest.raises(PolicyViolationError) as captured:
            backend._invocation_environment(agent, overrides)
        assert secret not in str(captured.value)
    assert backend.env == base_env
