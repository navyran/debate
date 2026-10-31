from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import pytest
from pydantic import BaseModel

from debate_agent.adapters import AdapterOutputError, VendorTerminal
from debate_agent.invocation_events import Redactor
from debate_agent.models import AgentName, AgentResult, Ballot, TurnResult
from debate_agent.process import ProcessResult
from debate_agent.protocol_gateway import (
    ProtocolBinding,
    ProtocolGateway,
    ProtocolNormalizationError,
    ProtocolNormalizer,
)
from debate_agent.review_models import (
    ReviewCoverageStatus,
    ReviewDimension,
    ReviewResult,
)


def process_result() -> ProcessResult:
    """构造成功进程结果；无参数，返回固定结果，不抛异常。"""
    return ProcessResult(
        return_code=0,
        stdout="vendor-terminal",
        stderr="",
        elapsed_seconds=1.0,
        raw_output_ref="raw/INV.json",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        cancelled=False,
    )


def turn_payload(*, decision: str = "accept") -> dict[str, object]:
    """构造完整 TurnResult；参数为结论，返回协议字典，不抛异常。"""
    return {
        "agent": "codex",
        "turn_id": "TURN-1",
        "base_commit": "abc",
        "observed_head": "abc",
        "goal_contract_version": 2,
        "decision": decision,
        "summary": "done",
        "accepted_points": [],
        "issues": [],
        "requested_scope_additions": [],
        "requested_validation_additions": [],
        "completion_assessment": "complete",
    }


def review_payload() -> dict[str, object]:
    """构造绑定当前 Supervisor 事实的最小 ReviewResult；无参数，返回字典。"""
    return {
        "agent": "codex",
        "turn_id": "TURN-1",
        "head_sha": "abc",
        "goal_contract_version": 2,
        "protocol_version": "review-v1",
        "coverage": [
            {
                "dimension": dimension.value,
                "status": ReviewCoverageStatus.UNKNOWN.value,
                "summary": f"待检查 {dimension.value}",
                "evidence_refs": [],
            }
            for dimension in ReviewDimension
        ],
        "findings": [],
        "issue_dispositions": [],
        "strongest_counterexample": None,
        "counterexample_resolution": None,
        "completion_assessment": "incomplete",
        "decision": "revise",
        "summary": "尚需检查证据",
    }


@dataclass
class FakeAdapter:
    """提供不解析厂商外壳的测试 Adapter。"""

    content: object
    agent: AgentName = AgentName.CODEX
    extracted: bool = False

    def extract_terminal(self, result: ProcessResult) -> VendorTerminal:
        """返回固定终止内容；参数为进程结果，返回终止对象，不抛异常。"""
        self.extracted = True
        return VendorTerminal(session_id="session-1", content=self.content)

    def failure_detail(self, stdout: str) -> str | None:
        """返回空失败详情；参数为 stdout，返回空，不抛异常。"""
        return None


class RecordingNormalizer(ProtocolNormalizer):
    """记录 Gateway 传入的原文并返回固定候选。"""

    def __init__(self, payload: dict[str, object]) -> None:
        """创建转码器；参数为固定载荷，返回实例，不抛异常。"""
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def normalize(
        self,
        *,
        raw_text: str,
        response_model: type[BaseModel],
        binding: ProtocolBinding,
        preserved_claims: Mapping[str, object],
    ) -> dict[str, object]:
        """记录转码输入；参数为 Gateway 上下文，返回固定载荷，不抛异常。"""
        self.calls.append(
            {
                "raw_text": raw_text,
                "response_model": response_model,
                "binding": binding,
                "preserved_claims": preserved_claims,
            }
        )
        return self.payload


class FailingNormalizer(ProtocolNormalizer):
    """模拟语义转码不可用。"""

    def normalize(
        self,
        *,
        raw_text: str,
        response_model: type[BaseModel],
        binding: ProtocolBinding,
        preserved_claims: Mapping[str, object],
    ) -> dict[str, object]:
        """抛出固定错误；参数为转码上下文，无返回值，始终抛协议转码错误。"""
        raise ProtocolNormalizationError("missing_api_key")


def binding() -> ProtocolBinding:
    """构造 Supervisor 绑定；无参数，返回固定绑定，不抛异常。"""
    return ProtocolBinding(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        head_sha="abc",
        goal_contract_version=2,
    )


def parse(
    adapter: FakeAdapter,
    *,
    normalizer: ProtocolNormalizer | None = None,
) -> AgentResult:
    """执行 Gateway；参数为 Adapter/转码器，返回 AgentResult，协议错误时抛错。"""
    return ProtocolGateway(normalizer=normalizer).parse_result(
        adapter=adapter,
        result=process_result(),
        response_model=TurnResult,
        binding=binding(),
        session_generation=1,
    )


def test_gateway_prefers_strict_json_and_records_protocol_method() -> None:
    adapter = FakeAdapter(json.dumps(turn_payload()))

    result = parse(adapter)

    assert result.protocol_method == "strict"
    assert result.structured_output == turn_payload()


def test_gateway_accepts_review_schema_with_supervisor_binding() -> None:
    """验证网关可严格解析 Reviewer 的 review.schema；无参数，无返回值。"""
    result = ProtocolGateway().parse_result(
        adapter=FakeAdapter(json.dumps(review_payload())),
        result=process_result(),
        response_model=ReviewResult,
        binding=binding(),
        session_generation=1,
    )

    assert result.protocol_method == "strict"
    assert result.structured_output == review_payload()


def test_binder_injects_supervisor_facts_before_pydantic_validation() -> None:
    payload = turn_payload()
    for field in (
        "agent",
        "turn_id",
        "base_commit",
        "observed_head",
        "goal_contract_version",
    ):
        payload.pop(field)

    result = parse(FakeAdapter(json.dumps(payload)))

    assert result.protocol_method == "strict"
    assert result.structured_output == turn_payload()


def test_gateway_applies_only_deterministic_quote_repair_before_llm() -> None:
    raw = json.dumps(turn_payload(), ensure_ascii=False).replace(
        '"summary": "done"',
        '"summary": "对"中国AI视频创业公司"重要"',
    )
    normalizer = RecordingNormalizer(turn_payload())

    result = parse(FakeAdapter(f"说明文字\n{raw}"), normalizer=normalizer)

    assert result.protocol_method == "deterministic"
    assert result.structured_output is not None
    assert result.structured_output["summary"] == '对"中国AI视频创业公司"重要'
    assert normalizer.calls == []


def test_gateway_uses_deepseek_only_after_deterministic_parsing_fails() -> None:
    raw = 'broken\n"decision":"accept"\n"completion_assessment":"complete"'
    normalizer = RecordingNormalizer(turn_payload())

    result = parse(FakeAdapter(raw), normalizer=normalizer)

    assert result.protocol_method == "deepseek"
    assert len(normalizer.calls) == 1
    assert normalizer.calls[0]["preserved_claims"] == {
        "decision": "accept",
        "completion_assessment": "complete",
    }


def test_gateway_never_uses_semantic_normalizer_to_construct_review_content() -> None:
    """验证 Review 损坏时不会让转码器补造 Coverage/Finding/Evidence。"""
    normalizer = RecordingNormalizer(review_payload())

    with pytest.raises(AdapterOutputError, match="review_semantics_require"):
        ProtocolGateway(normalizer=normalizer).parse_result(
            adapter=FakeAdapter('broken "decision":"revise"'),
            result=process_result(),
            response_model=ReviewResult,
            binding=binding(),
            session_generation=1,
        )

    assert normalizer.calls == []


def test_gateway_redacts_only_text_sent_to_deepseek() -> None:
    raw = (
        'TOKEN=secret\n"decision":"accept"\n'
        '"completion_assessment":"complete"'
    )
    normalizer = RecordingNormalizer(turn_payload())

    result = ProtocolGateway(normalizer=normalizer).parse_result(
        adapter=FakeAdapter(raw),
        result=process_result(),
        response_model=TurnResult,
        binding=binding(),
        session_generation=1,
        normalizer_text=lambda text: text.replace("secret", "<redacted>"),
    )

    assert result.protocol_method == "deepseek"
    assert "secret" not in normalizer.calls[0]["raw_text"]
    assert normalizer.calls[0]["preserved_claims"] == {
        "decision": "accept",
        "completion_assessment": "complete",
    }


def test_gateway_redacts_config_without_changing_true_for_normalizer() -> None:
    """验证转码输入仅遮罩配置值；无参数，无返回值。"""
    config_content = '{"permission":"allow","nonce":"high-entropy-0123456789"}'
    raw = (
        f"broken config={config_content}\n"
        'plain true and {"enabled":true}\n'
        '"decision":"accept"\n"completion_assessment":"complete"'
    )
    normalizer = RecordingNormalizer(turn_payload())

    result = ProtocolGateway(normalizer=normalizer).parse_result(
        adapter=FakeAdapter(raw),
        result=process_result(),
        response_model=TurnResult,
        binding=binding(),
        session_generation=1,
        normalizer_text=Redactor(
            workspace=None, secret_values=(config_content,)
        ).redact,
    )

    normalized_raw = normalizer.calls[0]["raw_text"]
    assert result.protocol_method == "deepseek"
    assert config_content not in normalized_raw
    assert "plain true" in normalized_raw
    assert '{"enabled":true}' in normalized_raw


def test_deepseek_cannot_change_agent_business_conclusion() -> None:
    raw = 'broken\n"decision":"accept"\n"completion_assessment":"complete"'
    normalizer = RecordingNormalizer(turn_payload(decision="revise"))

    with pytest.raises(AdapterOutputError, match="违反运行绑定"):
        parse(FakeAdapter(raw), normalizer=normalizer)


def test_deepseek_cannot_invent_missing_completion_claim() -> None:
    normalizer = RecordingNormalizer(turn_payload())

    with pytest.raises(AdapterOutputError, match="违反运行绑定"):
        parse(
            FakeAdapter('broken\n"decision":"accept"'),
            normalizer=normalizer,
        )


def test_deepseek_rejects_ambiguous_agent_conclusions() -> None:
    raw = (
        'broken\n"decision":"accept"\n"decision":"revise"\n'
        '"completion_assessment":"complete"'
    )

    with pytest.raises(AdapterOutputError, match="违反运行绑定"):
        parse(FakeAdapter(raw), normalizer=RecordingNormalizer(turn_payload()))


def test_deepseek_cannot_change_final_ballot_completion() -> None:
    raw = 'broken\n"completion":"complete"'
    normalizer = RecordingNormalizer(
        {
            "agent": "codex",
            "commit_sha": "abc",
            "goal_contract_version": 2,
            "completion": "incomplete",
            "criteria": [],
            "rationale": "not done",
        }
    )

    with pytest.raises(AdapterOutputError, match="违反运行绑定"):
        ProtocolGateway(normalizer=normalizer).parse_result(
            adapter=FakeAdapter(raw),
            result=process_result(),
            response_model=Ballot,
            binding=binding(),
            session_generation=1,
        )


def test_counterexample_ballot_rejects_semantic_normalizer_fabrication() -> None:
    """验证显式反例投票的缺字段原文不能由转码器补造为合格终局票。"""
    raw = 'broken\n"completion":"complete"'
    normalizer = RecordingNormalizer(
        {
            "agent": "codex",
            "commit_sha": "abc",
            "goal_contract_version": 2,
            "completion": "complete",
            "criteria": [],
            "rationale": "complete",
            "strongest_counterexample": "未发现能推翻结论的反例",
            "counterexample_resolution": "已读取当前验证报告。",
        }
    )

    with pytest.raises(
        AdapterOutputError,
        match="counterexample_ballot_requires_deterministic_parse",
    ):
        ProtocolGateway(
            normalizer=normalizer,
            counterexample_ballot_enabled=True,
        ).parse_result(
            adapter=FakeAdapter(raw),
            result=process_result(),
            response_model=Ballot,
            binding=binding(),
            session_generation=1,
        )

    assert normalizer.calls == []


def test_legacy_ballot_keeps_semantic_normalizer_compatibility() -> None:
    """验证未启用反例投票时沿用既有 Ballot 格式转码行为。"""
    raw = 'broken\n"completion":"complete"'
    normalizer = RecordingNormalizer(
        {
            "agent": "codex",
            "commit_sha": "abc",
            "goal_contract_version": 2,
            "completion": "complete",
            "criteria": [],
            "rationale": "complete",
        }
    )

    result = ProtocolGateway(normalizer=normalizer).parse_result(
        adapter=FakeAdapter(raw),
        result=process_result(),
        response_model=Ballot,
        binding=binding(),
        session_generation=1,
    )

    assert result.protocol_method == "deepseek"
    assert len(normalizer.calls) == 1


def test_gateway_exposes_sanitized_normalizer_failure_category() -> None:
    with pytest.raises(AdapterOutputError, match="missing_api_key"):
        parse(FakeAdapter("broken"), normalizer=FailingNormalizer())


def test_gateway_rejects_payload_that_conflicts_with_supervisor_binding() -> None:
    payload = turn_payload()
    payload["observed_head"] = "wrong"

    with pytest.raises(AdapterOutputError, match="违反运行绑定"):
        parse(FakeAdapter(json.dumps(payload)))


def test_process_failure_bypasses_vendor_terminal_and_protocol_layers() -> None:
    adapter = FakeAdapter("not-json")
    failed = replace(
        process_result(),
        return_code=7,
        stderr="provider failed",
    )

    result = ProtocolGateway().parse_result(
        adapter=adapter,
        result=failed,
        response_model=TurnResult,
        binding=binding(),
        session_generation=1,
    )

    assert result.ok is False
    assert result.error == "CLI 退出码 7: provider failed"
    assert adapter.extracted is False
