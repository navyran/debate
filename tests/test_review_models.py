from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    ReviewProtocolVersion,
)
from debate_agent.protocol_gateway import (
    InvariantBinder,
    ProtocolBinding,
    ProtocolInvariantError,
)
from debate_agent.review_models import (
    EvidenceKind,
    EvidenceRef,
    ReviewCoverageStatus,
    ReviewDimension,
    ReviewFinding,
    ReviewResult,
)


def evidence() -> dict[str, object]:
    """构造一条最小 Git 证据；无参数，返回协议字典，不抛异常。"""
    return {
        "kind": "git_path",
        "head_sha": "abc",
        "artifact_ref": None,
        "path": "src/debate_agent/runtime.py",
        "locator": "review_current_head",
    }


def coverage(
    dimension: ReviewDimension,
    *,
    status: ReviewCoverageStatus = ReviewCoverageStatus.PASS,
    evidence_refs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """构造一个核心覆盖项；参数为维度/状态，返回协议字典，不抛异常。"""
    if evidence_refs is None:
        evidence_refs = (
            [evidence()]
            if dimension
            in {ReviewDimension.GOAL_SCOPE, ReviewDimension.VALIDATION}
            else []
        )
    return {
        "dimension": dimension.value,
        "status": status.value,
        "summary": f"已检查 {dimension.value}",
        "evidence_refs": evidence_refs,
    }


def payload() -> dict[str, object]:
    """构造可完成的完整 ReviewResult；无参数，返回协议字典，不抛异常。"""
    return {
        "agent": "claude",
        "turn_id": "REVIEW-TURN-1",
        "head_sha": "abc",
        "goal_contract_version": 2,
        "protocol_version": "review-v1",
        "coverage": [coverage(dimension) for dimension in ReviewDimension],
        "findings": [],
        "issue_dispositions": [],
        "strongest_counterexample": "恢复路径可能遗漏协议版本分支",
        "counterexample_resolution": "已检查恢复分支并保留 legacy 兼容逻辑",
        "completion_assessment": "complete",
        "decision": "accept",
        "summary": "四个核心审查面均已检查",
    }


def test_review_result_round_trips_with_all_protocol_fields() -> None:
    """验证完整审查结果可序列化；无参数，无返回值，非法模型时抛校验错误。"""
    result = ReviewResult.model_validate(payload())

    assert result.agent is AgentName.CLAUDE
    assert result.protocol_version is ReviewProtocolVersion.REVIEW_V1
    assert result.model_dump(mode="json") == payload()


def test_review_schema_matches_review_result_fields_and_enums() -> None:
    """验证静态 Schema 与 Pydantic 协议字段一致；无参数，无返回值。"""
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "review.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    model_schema = ReviewResult.model_json_schema()

    assert set(schema["required"]) == set(model_schema["required"])
    assert set(schema["properties"]) == set(model_schema["properties"])
    assert schema["properties"]["agent"]["enum"] == [
        agent.value for agent in AgentName
    ]
    assert schema["properties"]["protocol_version"]["const"] == "review-v1"
    coverage_schema = schema["properties"]["coverage"]["items"]
    assert coverage_schema["properties"]["dimension"]["enum"] == [
        dimension.value for dimension in ReviewDimension
    ]
    assert coverage_schema["properties"]["status"]["enum"] == [
        status.value for status in ReviewCoverageStatus
    ]


def test_review_schema_declares_types_for_ajv_strict_keywords() -> None:
    """验证条件 Schema 对 AJV strictTypes 所需的 object/array 类型显式声明。"""
    schema_path = (
        Path(__file__).resolve().parents[1] / "schemas" / "review.schema.json"
    )
    root = json.loads(schema_path.read_text(encoding="utf-8"))

    def walk(node: object) -> None:
        """递归检查 Schema 节点；参数为节点，无返回值，严格类型缺失时断言失败。"""
        if isinstance(node, dict):
            declared = node.get("type")
            types = set(declared) if isinstance(declared, list) else {declared}
            if "properties" in node or "required" in node:
                assert "object" in types
            if any(key in node for key in ("contains", "minItems", "maxItems")):
                assert "array" in types
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(root)


def schema_validator() -> Draft202012Validator:
    """加载静态 Draft 2020-12 Schema；无参数，返回校验器，Schema 非法时抛异常。"""
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "review.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_json_schema_accepts_valid_complete_review() -> None:
    """验证静态 Schema 接受合法完成结论；无参数，无返回值。"""
    schema_validator().validate(payload())


@pytest.mark.parametrize(
    "invalid_coverage",
    [
        [
            coverage(ReviewDimension.GOAL_SCOPE),
            coverage(ReviewDimension.CORRECTNESS),
            coverage(ReviewDimension.INTEGRATION),
        ],
        [
            coverage(ReviewDimension.GOAL_SCOPE),
            coverage(ReviewDimension.GOAL_SCOPE),
            coverage(ReviewDimension.INTEGRATION),
            coverage(ReviewDimension.VALIDATION),
        ],
    ],
)
def test_review_result_rejects_missing_or_duplicate_core_coverage(
    invalid_coverage: list[dict[str, object]],
) -> None:
    """验证四个核心覆盖项不可缺失或重复；参数为非法覆盖，无返回值。"""
    invalid_payload = payload()
    invalid_payload["coverage"] = invalid_coverage

    with pytest.raises(ValidationError, match="ReviewCoverage"):
        ReviewResult.model_validate(invalid_payload)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator().validate(invalid_payload)


@pytest.mark.parametrize(
    "change",
    [
        {
            "coverage": [
                coverage(
                    dimension,
                    status=(
                        ReviewCoverageStatus.UNKNOWN
                        if dimension is ReviewDimension.INTEGRATION
                        else ReviewCoverageStatus.PASS
                    ),
                )
                for dimension in ReviewDimension
            ]
        },
        {
            "findings": [
                {
                    "finding_id": "FIND-1",
                    "dimension": "integration",
                    "claim": "恢复路径使用错误字段",
                    "impact": "恢复失败",
                    "blocking": True,
                    "evidence_refs": [evidence()],
                    "proposed_change": "保留旧字段读取",
                }
            ]
        },
        {"strongest_counterexample": None},
        {"counterexample_resolution": None},
    ],
)
def test_review_result_rejects_illegal_complete_assessment(
    change: dict[str, object],
) -> None:
    """验证完成结论必须消除未知、阻断与最强反例；参数为非法改动，无返回值。"""
    invalid_payload = payload()
    invalid_payload.update(change)

    with pytest.raises(ValidationError):
        ReviewResult.model_validate(invalid_payload)


def test_complete_review_requires_goal_scope_and_validation_evidence() -> None:
    """验证完成结论必须有范围和验证证据；无参数，无返回值。"""
    for dimension in (ReviewDimension.GOAL_SCOPE, ReviewDimension.VALIDATION):
        invalid_payload = payload()
        invalid_payload["coverage"] = [
            coverage(
                item,
                evidence_refs=[] if item is dimension else None,
            )
            for item in ReviewDimension
        ]

        with pytest.raises(ValidationError, match="goal_scope 和 validation"):
            ReviewResult.model_validate(invalid_payload)
        with pytest.raises(JsonSchemaValidationError):
            schema_validator().validate(invalid_payload)


def test_json_schema_rejects_complete_cross_field_violations() -> None:
    """验证静态 Schema 拒绝所有完成态跨字段冲突；无参数，无返回值。"""
    invalid_payloads: list[dict[str, object]] = []

    unknown_coverage = payload()
    unknown_coverage["coverage"] = [
        coverage(
            dimension,
            status=(
                ReviewCoverageStatus.UNKNOWN
                if dimension is ReviewDimension.INTEGRATION
                else ReviewCoverageStatus.PASS
            ),
        )
        for dimension in ReviewDimension
    ]
    invalid_payloads.append(unknown_coverage)

    blocking_finding = payload()
    blocking_finding["findings"] = [
        {
            "finding_id": "FIND-1",
            "dimension": "integration",
            "claim": "恢复路径使用错误字段",
            "impact": "恢复失败",
            "blocking": True,
            "evidence_refs": [evidence()],
            "proposed_change": "保留旧字段读取",
        }
    ]
    invalid_payloads.append(blocking_finding)

    missing_counterexample = payload()
    missing_counterexample["strongest_counterexample"] = None
    invalid_payloads.append(missing_counterexample)

    missing_resolution = payload()
    missing_resolution["counterexample_resolution"] = None
    invalid_payloads.append(missing_resolution)

    validator = schema_validator()
    for invalid_payload in invalid_payloads:
        with pytest.raises(JsonSchemaValidationError):
            validator.validate(invalid_payload)


def test_blocking_finding_requires_machine_evidence() -> None:
    """验证阻断发现必须带 EvidenceRef；无参数，无返回值。"""
    with pytest.raises(ValidationError, match="EvidenceRef"):
        ReviewFinding(
            finding_id="FIND-1",
            dimension=ReviewDimension.CORRECTNESS,
            claim="主要路径未处理空输入",
            impact="用户请求会失败",
            blocking=True,
            evidence_refs=[],
            proposed_change="补充空输入分支",
        )

    invalid_payload = payload()
    invalid_payload["completion_assessment"] = "incomplete"
    invalid_payload["findings"] = [
        {
            "finding_id": "FIND-1",
            "dimension": "correctness",
            "claim": "主要路径未处理空输入",
            "impact": "用户请求会失败",
            "blocking": True,
            "evidence_refs": [],
            "proposed_change": "补充空输入分支",
        }
    ]
    with pytest.raises(JsonSchemaValidationError):
        schema_validator().validate(invalid_payload)


def test_evidence_ref_requires_kind_specific_structural_reference() -> None:
    """验证证据只做类型结构校验；无参数，无返回值。"""
    with pytest.raises(ValidationError, match="path"):
        EvidenceRef(
            kind=EvidenceKind.GIT_PATH,
            head_sha="abc",
            artifact_ref=None,
            path=None,
            locator=None,
        )
    with pytest.raises(ValidationError, match="artifact_ref"):
        EvidenceRef(
            kind=EvidenceKind.TEST_RESULT,
            head_sha="abc",
            artifact_ref=None,
            path="tests/test_runtime.py",
            locator=None,
        )


def test_generic_gateway_binder_injects_review_binding_and_rejects_conflict() -> None:
    """验证 Gateway 泛型绑定适用于 ReviewResult；无参数，无返回值。"""
    binding = ProtocolBinding(
        agent=AgentName.CLAUDE,
        turn_id="REVIEW-TURN-1",
        head_sha="abc",
        goal_contract_version=2,
    )
    bound = InvariantBinder.bind(
        payload(),
        response_model=ReviewResult,
        binding=binding,
        preserved_claims={},
        require_semantic_claims=False,
    )

    assert ReviewResult.model_validate(bound).head_sha == "abc"
    wrong = payload()
    wrong["head_sha"] = "wrong"
    with pytest.raises(ProtocolInvariantError, match="Supervisor"):
        InvariantBinder.bind(
            wrong,
            response_model=ReviewResult,
            binding=binding,
            preserved_claims={},
            require_semantic_claims=False,
        )


def test_non_blocking_observation_does_not_require_evidence_or_block_completion(
) -> None:
    """验证非阻断观察可无证据且不妨碍完成；无参数，无返回值。"""
    accepted_payload = payload()
    accepted_payload["findings"] = [
        {
            "finding_id": "FIND-OBS-1",
            "dimension": "validation",
            "claim": "测试命名可更简洁",
            "impact": "可读性略受影响",
            "blocking": False,
            "evidence_refs": [],
            "proposed_change": None,
        }
    ]

    assert ReviewResult.model_validate(
        accepted_payload
    ).completion_assessment is CompletionAssessment.COMPLETE
