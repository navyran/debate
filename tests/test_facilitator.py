from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.facilitator import (
    FacilitatorService,
    build_facilitator_decision_id,
    compile_facilitator_hint,
    select_facilitator_mode,
)
from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    Decision,
    FacilitatorAction,
    FacilitatorContext,
    FacilitatorDecision,
    FacilitatorMode,
    FacilitatorPurpose,
    FacilitatorRecord,
    FacilitatorStatus,
    FacilitatorTurnSummary,
)


class FakeStructuredModel:
    """只替代 DeepSeek 网络边界并返回完整协议对象。"""

    def __init__(
        self,
        result: FacilitatorDecision | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[object] = []

    def invoke(self, value: object) -> FacilitatorDecision:
        """记录完整请求并返回协议结果；参数为消息，返回决定，配置异常时抛错。"""
        self.calls.append(value)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def facilitator_config(tmp_path: Path, **updates: object) -> RunConfig:
    """创建测试配置；参数为临时目录/覆盖值，返回冻结配置。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return RunConfig(
        workspace=tmp_path,
        topic="完成目标",
        state_root=tmp_path / "state",
        **updates,
    )


def turn(
    agent: AgentName,
    completion: CompletionAssessment = CompletionAssessment.INCOMPLETE,
) -> FacilitatorTurnSummary:
    """创建脱敏回合摘要；参数为 Agent/完成状态，返回摘要。"""
    return FacilitatorTurnSummary(
        agent=agent,
        decision=Decision.REVISE,
        summary=f"{agent.value} 审查结果",
        issues=(
            ["tests pass 尚未满足"]
            if completion is CompletionAssessment.INCOMPLETE
            else []
        ),
        completion_assessment=completion,
    )


def facilitator_context(
    *,
    purpose: FacilitatorPurpose = FacilitatorPurpose.TURN_ROUTING,
    stall_count: int = 0,
    codex_completion: CompletionAssessment = CompletionAssessment.INCOMPLETE,
    claude_completion: CompletionAssessment = CompletionAssessment.INCOMPLETE,
) -> FacilitatorContext:
    """创建完整语义上下文；参数为目的/停滞/双方状态，返回上下文。"""
    return FacilitatorContext(
        run_id="RUN-1",
        purpose=purpose,
        turn_id_or_revision="TURN-1",
        head_sha="abc123",
        goal_contract_version=2,
        objective="完成目标",
        deliverables=["src/app.py"],
        acceptance_criteria=["tests pass"],
        effective_scope=["src", "tests"],
        writer=turn(AgentName.CODEX, codex_completion),
        reviewer=turn(AgentName.CLAUDE, claude_completion),
        changed_paths=["src/app.py"],
        behavior_summary=["增加实现"],
        validation_passed=False,
        validation_report_ref="validation/TURN-1.json",
        assessments={
            AgentName.CODEX: codex_completion,
            AgentName.CLAUDE: claude_completion,
        },
        stall_count=stall_count,
        remaining_turns=4,
        audit_disputes=["审计摘要遗漏"]
        if purpose is FacilitatorPurpose.AUDIT_DISPUTE
        else [],
    )


def valid_decision(
    context: FacilitatorContext,
    *,
    action: FacilitatorAction | None = None,
    next_writer: AgentName | None = AgentName.CLAUDE,
) -> FacilitatorDecision:
    """创建与上下文绑定的决定；参数为上下文/动作/Writer，返回协议对象。"""
    selected_action = action or (
        FacilitatorAction.REGENERATE_AUDIT
        if context.purpose is FacilitatorPurpose.AUDIT_DISPUTE
        else FacilitatorAction.CONTINUE_DEBATE
    )
    return FacilitatorDecision(
        decision_id=build_facilitator_decision_id(context),
        purpose=context.purpose,
        head_sha=context.head_sha,
        goal_contract_version=context.goal_contract_version,
        mode=select_facilitator_mode(context),
        action=selected_action,
        recommended_next_writer=next_writer,
        next_instruction="先读取当前 HEAD，再修复失败验收条件",
        unresolved_criteria=["tests pass"],
        risk_flags=[],
        evidence_refs=["validation/TURN-1.json"],
    )


def test_mode_is_deterministic_for_routine_conflict_stall_and_audit() -> None:
    assert (
        select_facilitator_mode(facilitator_context())
        is FacilitatorMode.NON_THINKING
    )
    assert (
        select_facilitator_mode(facilitator_context(stall_count=1))
        is FacilitatorMode.THINKING
    )
    assert (
        select_facilitator_mode(
            facilitator_context(
                codex_completion=CompletionAssessment.COMPLETE,
                claude_completion=CompletionAssessment.INCOMPLETE,
            )
        )
        is FacilitatorMode.THINKING
    )
    assert (
        select_facilitator_mode(
            facilitator_context(purpose=FacilitatorPurpose.AUDIT_DISPUTE)
        )
        is FacilitatorMode.THINKING
    )


def test_evaluate_persists_valid_structured_decision_once(tmp_path: Path) -> None:
    config = facilitator_config(tmp_path)
    context = facilitator_context()
    model = FakeStructuredModel(valid_decision(context))
    observed_keys: list[SecretStr] = []

    def factory(mode: FacilitatorMode, key: SecretStr) -> FakeStructuredModel:
        observed_keys.append(key)
        assert mode is FacilitatorMode.NON_THINKING
        return model

    service = FacilitatorService(
        config=config,
        artifacts=ArtifactStore.create(config, run_id="RUN-1"),
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
        model_factory=factory,
    )

    first = service.evaluate(context)
    second = service.evaluate(context)

    assert first.status is FacilitatorStatus.DECISION
    assert second == first
    assert len(model.calls) == 1
    assert observed_keys[0].get_secret_value() == "actual-secret-value"
    assert "actual-secret-value" not in first.model_dump_json()


def test_missing_key_falls_back_without_creating_model(tmp_path: Path) -> None:
    config = facilitator_config(tmp_path)
    service = FacilitatorService(
        config=config,
        artifacts=ArtifactStore.create(config, run_id="RUN-1"),
        environment={},
        model_factory=lambda mode, key: (_ for _ in ()).throw(
            AssertionError("model must not be created")
        ),
    )

    record = service.evaluate(facilitator_context())

    assert record.status is FacilitatorStatus.FALLBACK
    assert record.error_category == "missing_api_key"
    assert record.decision is None


def test_model_uses_json_mode_for_deepseek_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 DeepSeek v4 thinking 参数通过 SDK 支持的 extra_body 发送。"""
    captured: dict[str, object] = {}

    class FakeChatModel:
        """记录结构化输出配置；无业务副作用。"""

        def with_structured_output(self, schema: object, *, method: str) -> object:
            """返回自身；参数为 Schema/方法，供工厂测试，不抛异常。"""
            captured["schema"] = schema
            captured["method"] = method
            return self

    def fake_chat_deepseek(**kwargs: object) -> FakeChatModel:
        """捕获模型构造参数；参数为关键字，返回假模型。"""
        captured.update(kwargs)
        return FakeChatModel()

    monkeypatch.setattr("debate_agent.facilitator.ChatDeepSeek", fake_chat_deepseek)
    config = facilitator_config(tmp_path)
    service = FacilitatorService(
        config=config,
        artifacts=ArtifactStore.create(config, run_id="RUN-1"),
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
    )

    service._create_model(FacilitatorMode.NON_THINKING, SecretStr("secret"))

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["method"] == "function_calling"
    assert "model_kwargs" not in captured

    service._create_model(FacilitatorMode.THINKING, SecretStr("secret"))

    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["reasoning_effort"] == "high"
    assert captured["method"] == "json_mode"


def test_messages_bind_json_mode_identity_fields() -> None:
    """验证 JSON 模式显式绑定身份字段；无参数和返回值。"""
    context = facilitator_context(stall_count=1)
    decision_id = build_facilitator_decision_id(context)

    messages = FacilitatorService._messages(
        context, decision_id, FacilitatorMode.THINKING
    )
    payload = json.loads(messages[1][1])

    assert payload["required_binding"] == {
        "decision_id": decision_id,
        "purpose": context.purpose.value,
        "head_sha": context.head_sha,
        "goal_contract_version": context.goal_contract_version,
        "mode": FacilitatorMode.THINKING.value,
    }


def test_timeout_and_stale_binding_fall_back_without_error_body(tmp_path: Path) -> None:
    timeout_config = facilitator_config(tmp_path / "timeout")
    timeout_model = FakeStructuredModel(error=TimeoutError("TOKEN=secret-body"))
    timeout_service = FacilitatorService(
        config=timeout_config,
        artifacts=ArtifactStore.create(timeout_config, run_id="RUN-1"),
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
        model_factory=lambda mode, key: timeout_model,
    )
    timeout_record = timeout_service.evaluate(facilitator_context())
    assert timeout_record.error_category == "timeout"
    assert "secret-body" not in timeout_record.model_dump_json()

    stale_root = tmp_path / "stale"
    stale_config = facilitator_config(stale_root)
    context = facilitator_context()
    stale = valid_decision(context).model_copy(update={"head_sha": "old-head"})
    stale_service = FacilitatorService(
        config=stale_config,
        artifacts=ArtifactStore.create(stale_config, run_id="RUN-1"),
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
        model_factory=lambda mode, key: FakeStructuredModel(stale),
    )
    assert stale_service.evaluate(context).error_category == "stale_binding"


def test_provider_http_400_is_invalid_request_without_error_body(
    tmp_path: Path,
) -> None:
    """验证 Provider 400 单独分类且不落响应；参数为临时目录，无返回值。"""

    class ProviderBadRequest(Exception):
        """模拟带 HTTP 状态的 Provider 异常。"""

        status_code = 400

    config = facilitator_config(tmp_path)
    service = FacilitatorService(
        config=config,
        artifacts=ArtifactStore.create(config, run_id="RUN-1"),
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
        model_factory=lambda mode, key: FakeStructuredModel(
            error=ProviderBadRequest("TOKEN=secret-provider-body")
        ),
    )

    record = service.evaluate(facilitator_context())

    assert record.error_category == "invalid_request"
    assert "secret-provider-body" not in record.model_dump_json()


def test_illegal_action_for_purpose_is_policy_fallback(tmp_path: Path) -> None:
    config = facilitator_config(tmp_path)
    context = facilitator_context()
    decision = valid_decision(context, action=FacilitatorAction.REGENERATE_AUDIT)
    service = FacilitatorService(
        config=config,
        artifacts=ArtifactStore.create(config, run_id="RUN-1"),
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
        model_factory=lambda mode, key: FakeStructuredModel(decision),
    )

    record = service.evaluate(context)

    assert record.status is FacilitatorStatus.FALLBACK
    assert record.error_category == "policy_rejection"


def test_prepared_record_recovers_as_uncertain_fallback(tmp_path: Path) -> None:
    config = facilitator_config(tmp_path)
    artifacts = ArtifactStore.create(config, run_id="RUN-1")
    context = facilitator_context()
    decision_id = build_facilitator_decision_id(context)
    artifacts.write_facilitator_record(
        decision_id,
        {
            "decision_id": decision_id,
            "purpose": context.purpose.value,
            "head_sha": context.head_sha,
            "goal_contract_version": context.goal_contract_version,
            "mode": select_facilitator_mode(context).value,
            "status": FacilitatorStatus.PREPARED.value,
            "decision": None,
            "error_category": None,
            "deterministic_route": None,
        },
    )
    service = FacilitatorService(
        config=config,
        artifacts=artifacts,
        environment={"DEEPSEEK_API_KEY": "actual-secret-value"},
        model_factory=lambda mode, key: (_ for _ in ()).throw(
            AssertionError("uncertain call must not be repeated")
        ),
    )

    record = service.evaluate(context)

    assert record.status is FacilitatorStatus.FALLBACK
    assert record.error_category == "uncertain_previous_call"


def test_hint_compiler_respects_agent_completion_assessments() -> None:
    context = facilitator_context()
    decision = valid_decision(context, next_writer=AgentName.CLAUDE)
    record = FacilitatorRecord(
        decision_id=decision.decision_id,
        purpose=context.purpose,
        head_sha=context.head_sha,
        goal_contract_version=context.goal_contract_version,
        mode=decision.mode,
        status=FacilitatorStatus.DECISION,
        decision=decision,
    )
    writer, instruction = compile_facilitator_hint(
        record,
        assessments={
            AgentName.CODEX: CompletionAssessment.INCOMPLETE,
            AgentName.CLAUDE: CompletionAssessment.COMPLETE,
        },
    )
    assert writer is None
    assert instruction == decision.next_instruction

    writer, _ = compile_facilitator_hint(
        record,
        assessments={agent: CompletionAssessment.COMPLETE for agent in AgentName},
    )
    assert writer is None
