from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from debate_agent.config import RunConfig
from debate_agent.models import AgentName, Ballot, ReviewProtocolVersion, TurnResult
from debate_agent.protocol_gateway import ProtocolBinding
from debate_agent.protocol_normalizer import (
    DeepSeekProtocolNormalizer,
    ProtocolNormalizationError,
)
from debate_agent.review_models import ReviewResult


class FakeStructuredModel:
    """返回固定结构化协议并记录 LangChain 消息。"""

    def __init__(self, payload: dict[str, object]) -> None:
        """创建模型；参数为固定载荷，返回实例，不抛异常。"""
        self.payload = payload
        self.messages: object = None

    def invoke(
        self,
        input: Any,
        config: Any | None = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        """记录并返回协议；参数为消息/配置，返回字典，不抛异常。"""
        self.messages = input
        return self.payload


def payload() -> dict[str, object]:
    """构造完整 TurnResult；无参数，返回协议字典，不抛异常。"""
    return {
        "agent": "codex",
        "turn_id": "TURN-1",
        "base_commit": "abc",
        "observed_head": "abc",
        "goal_contract_version": 2,
        "decision": "accept",
        "summary": "done",
        "accepted_points": [],
        "issues": [],
        "requested_scope_additions": [],
        "requested_validation_additions": [],
        "completion_assessment": "complete",
    }


def review_payload() -> dict[str, object]:
    """构造最小 ReviewResult；无参数，返回字典，不抛异常。"""
    return {
        "agent": "codex",
        "turn_id": "TURN-1",
        "head_sha": "abc",
        "goal_contract_version": 2,
        "protocol_version": "review-v1",
        "coverage": [
            {
                "dimension": dimension,
                "status": "unknown",
                "summary": f"待检查 {dimension}",
                "evidence_refs": [],
            }
            for dimension in ("goal_scope", "correctness", "integration", "validation")
        ],
        "findings": [],
        "issue_dispositions": [],
        "strongest_counterexample": None,
        "counterexample_resolution": None,
        "completion_assessment": "incomplete",
        "decision": "revise",
        "summary": "需要继续审查",
    }


def binding() -> ProtocolBinding:
    """构造协议绑定；无参数，返回固定绑定，不抛异常。"""
    return ProtocolBinding(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        head_sha="abc",
        goal_contract_version=2,
    )


def test_normalizer_uses_configured_deepseek_structured_output(
    tmp_path: Path,
) -> None:
    model = FakeStructuredModel(payload())
    captured: dict[str, object] = {}

    def model_factory(
        target_schema: dict[str, Any], api_key: SecretStr
    ) -> FakeStructuredModel:
        """记录模型工厂参数；参数为 JSON Schema/Key，返回假模型，不抛异常。"""
        captured.update(target_schema=target_schema, api_key=api_key)
        return model

    normalizer = DeepSeekProtocolNormalizer(
        config=RunConfig(workspace=tmp_path, topic="goal"),
        environment={"DEEPSEEK_API_KEY": "secret"},
        model_factory=model_factory,
    )

    result = normalizer.normalize(
        raw_text='broken "decision":"accept"',
        response_model=TurnResult,
        binding=binding(),
        preserved_claims={"decision": "accept"},
    )

    assert result == payload()
    assert captured["target_schema"]["title"] == "TurnResult"
    assert isinstance(captured["api_key"], SecretStr)
    assert isinstance(model.messages, list)
    human = json.loads(model.messages[1][1])
    assert human["required_binding"]["observed_head"] == "abc"
    assert human["preserved_claims"] == {"decision": "accept"}


def test_normalizer_requires_api_key(tmp_path: Path) -> None:
    normalizer = DeepSeekProtocolNormalizer(
        config=RunConfig(workspace=tmp_path, topic="goal"),
        environment={},
    )

    with pytest.raises(ProtocolNormalizationError, match="missing_api_key"):
        normalizer.normalize(
            raw_text="broken",
            response_model=TurnResult,
            binding=binding(),
            preserved_claims={},
        )


def test_normalizer_refuses_to_construct_review_semantics(tmp_path: Path) -> None:
    """验证转码器不能补造 Review 的 Coverage/Finding/Evidence；参数为临时目录。"""
    model = FakeStructuredModel(review_payload())
    normalizer = DeepSeekProtocolNormalizer(
        config=RunConfig(workspace=tmp_path, topic="goal"),
        environment={"DEEPSEEK_API_KEY": "secret"},
        model_factory=lambda schema, key: model,
    )

    with pytest.raises(
        ProtocolNormalizationError,
        match="review_semantics_require_deterministic_parse",
    ):
        normalizer.normalize(
            raw_text="broken ReviewResult",
            response_model=ReviewResult,
            binding=binding(),
            preserved_claims={},
        )

    assert model.messages is None


def test_normalizer_refuses_counterexample_ballot_semantics(tmp_path: Path) -> None:
    """验证直接调用转码器也不能补造显式反例终局投票字段。"""
    model = FakeStructuredModel({})
    normalizer = DeepSeekProtocolNormalizer(
        config=RunConfig(
            workspace=tmp_path,
            topic="反例终局投票",
            protocol_version=ReviewProtocolVersion.REVIEW_V1,
            counterexample_ballot=True,
        ),
        environment={"DEEPSEEK_API_KEY": "secret"},
        model_factory=lambda schema, key: model,
    )

    with pytest.raises(
        ProtocolNormalizationError,
        match="counterexample_ballot_requires_deterministic_parse",
    ):
        normalizer.normalize(
            raw_text='broken "completion":"complete"',
            response_model=Ballot,
            binding=binding(),
            preserved_claims={"completion": "complete"},
        )

    assert model.messages is None


def test_normalizer_can_be_disabled(tmp_path: Path) -> None:
    normalizer = DeepSeekProtocolNormalizer(
        config=RunConfig(
            workspace=tmp_path,
            topic="goal",
            protocol_normalizer_enabled=False,
        ),
        environment={"DEEPSEEK_API_KEY": "secret"},
    )

    with pytest.raises(
        ProtocolNormalizationError,
        match="protocol_normalizer_disabled",
    ):
        normalizer.normalize(
            raw_text="broken",
            response_model=TurnResult,
            binding=binding(),
            preserved_claims={},
        )


def test_normalizer_sanitizes_provider_failure(tmp_path: Path) -> None:
    def model_factory(
        target_schema: dict[str, Any], api_key: SecretStr
    ) -> FakeStructuredModel:
        """模拟模型创建失败；参数为 Schema/Key，无返回值，始终抛连接错误。"""
        raise ConnectionError("secret provider detail")

    normalizer = DeepSeekProtocolNormalizer(
        config=RunConfig(workspace=tmp_path, topic="goal"),
        environment={"DEEPSEEK_API_KEY": "secret"},
        model_factory=model_factory,
    )

    with pytest.raises(ProtocolNormalizationError, match="^provider_error$"):
        normalizer.normalize(
            raw_text="broken",
            response_model=TurnResult,
            binding=binding(),
            preserved_claims={},
        )
