from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import (
    AgentName,
    AgentResult,
    AgentSessionState,
    Ballot,
    ChangeAck,
    ChangeSummary,
    CompletionAssessment,
    DebateState,
    Decision,
    FacilitatorAction,
    FacilitatorDecision,
    FacilitatorMode,
    FacilitatorPurpose,
    LeaseState,
    LeaseStatus,
    PeerMemoryUpdate,
    ReviewProtocolVersion,
    RunStatus,
    SessionHealth,
    TreeAck,
    TurnResult,
)
from debate_agent.review_models import ReviewResult


def test_run_config_builds_from_inline_topic_and_freezes_profiles(
    tmp_path: Path,
) -> None:
    config = RunConfig.from_sources(workspace=tmp_path, topic="实现功能")

    assert config.workspace == tmp_path.resolve()
    assert config.topic == "实现功能"
    assert config.protocol_version is ReviewProtocolVersion.LEGACY_V0
    assert config.issue_ledger_enabled is False
    assert config.counterexample_ballot is False
    assert config.first_agent is AgentName.CODEX
    assert config.participants == (AgentName.CODEX, AgentName.CLAUDE)
    assert config.profiles[AgentName.QODER].command == "qodercli"
    assert config.max_turns == 8
    assert config.total_timeout_seconds == 7200
    assert config.codex_resume_sandbox_verified is True
    assert config.unrestricted_commands is True
    assert config.facilitator_enabled is True
    assert config.protocol_normalizer_enabled is True
    assert config.facilitator_model == "deepseek-v4-flash"
    assert config.facilitator_base_url == "https://api.deepseek.com"
    assert config.facilitator_timeout_seconds == 120
    assert config.facilitator_api_key_env == "DEEPSEEK_API_KEY"
    assert config.max_process_failures == 2
    assert config.health_probe_timeout_seconds == 60
    assert config.circuit_breaker_threshold == 2
    assert config.circuit_breaker_window_seconds == 600
    assert config.circuit_breaker_cooldown_seconds == 300
    assert config.realtime_output_enabled is True
    assert config.raw_output_tail_lines == 500
    assert config.mcp_mode == "minimal"
    assert "actual-secret-value" not in config.model_dump_json()
    for profile in config.profiles.values():
        assert profile.startup_timeout_seconds == 300
        assert profile.idle_timeout_seconds == 300
        assert profile.hard_timeout_seconds == 3600
        assert profile.heartbeat_interval_seconds == 5
        assert profile.effective_hard_timeout_seconds == 3600
    with pytest.raises(ValidationError):
        config.max_turns = 3


def test_run_config_migrates_missing_protocol_version_and_serializes_only_it(
    tmp_path: Path,
) -> None:
    """验证旧配置迁移和新冻结配置的唯一协议字段；参数为临时目录，无返回值。"""
    legacy = RunConfig.model_validate(
        {
            "workspace": str(tmp_path),
            "topic": "恢复旧 Run",
        }
    )

    assert legacy.protocol_version is ReviewProtocolVersion.LEGACY_V0
    assert "protocol_version" in legacy.model_dump(mode="json")
    assert "review_protocol_version" not in legacy.model_dump(mode="json")

    review_v1 = RunConfig(
        workspace=tmp_path,
        topic="新协议 Run",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
    )
    store = ArtifactStore.create(review_v1, run_id="RUN-PROTOCOL")
    payload = json.loads(store.config_path.read_text(encoding="utf-8"))

    assert payload["protocol_version"] == "review-v1"
    assert payload["issue_ledger_enabled"] is False
    assert payload["counterexample_ballot"] is False
    assert "review_protocol_version" not in payload
    assert store.load_config().protocol_version is ReviewProtocolVersion.REVIEW_V1


def test_run_config_rejects_invalid_or_duplicate_protocol_fields(
    tmp_path: Path,
) -> None:
    """验证协议版本严格校验；参数为临时目录，无返回值，非法字段时抛校验错误。"""
    with pytest.raises(ValidationError):
        RunConfig(
            workspace=tmp_path,
            topic="非法协议",
            protocol_version="review-v2",
        )


def test_run_config_rejects_issue_ledger_for_legacy_protocol(tmp_path: Path) -> None:
    """验证 Ledger 必须显式启用 review-v1；参数为临时目录，无返回值。"""
    with pytest.raises(ValidationError, match="仅支持 review-v1"):
        RunConfig(
            workspace=tmp_path,
            topic="旧协议账本",
            issue_ledger_enabled=True,
        )
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "workspace": str(tmp_path),
                "topic": "双协议字段",
                "protocol_version": "review-v1",
                "review_protocol_version": "review-v1",
            }
        )


def test_run_config_rejects_counterexample_ballot_for_legacy_protocol(
    tmp_path: Path,
) -> None:
    """验证反例式 Blind Ballot 只能由 review-v1 显式启用。"""
    with pytest.raises(ValidationError, match="反例式 Blind Ballot 仅支持 review-v1"):
        RunConfig(
            workspace=tmp_path,
            topic="旧协议反例投票",
            counterexample_ballot=True,
        )


def test_run_config_accepts_selected_qoder_and_rejects_invalid_participants(
    tmp_path: Path,
) -> None:
    config = RunConfig(
        workspace=tmp_path,
        topic="三方讨论",
        participants=(AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex"),
            AgentName.CLAUDE: AgentProfile(command="claude"),
            AgentName.QODER: AgentProfile(command="qodercli"),
        },
    )

    assert config.participants[-1] is AgentName.QODER
    with pytest.raises(ValidationError, match="至少需要两个"):
        RunConfig(
            workspace=tmp_path,
            topic="单方",
            participants=(AgentName.QODER,),
            first_agent=AgentName.QODER,
        )
    with pytest.raises(ValidationError, match="first_agent"):
        RunConfig(
            workspace=tmp_path,
            topic="首位未参与",
            participants=(AgentName.CLAUDE, AgentName.QODER),
        )


def test_run_config_reads_topic_file_and_rejects_ambiguous_sources(
    tmp_path: Path,
) -> None:
    topic_file = tmp_path / "topic.md"
    topic_file.write_text("  从文件读取目标  ", encoding="utf-8")

    config = RunConfig.from_sources(workspace=tmp_path, topic_file=topic_file)

    assert config.topic == "从文件读取目标"
    with pytest.raises(ValueError, match="只能指定一种"):
        RunConfig.from_sources(workspace=tmp_path, topic="内联", topic_file=topic_file)
    with pytest.raises(ValueError, match="必须提供"):
        RunConfig.from_sources(workspace=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_turns", 0),
        ("total_timeout_seconds", 0),
        ("max_process_failures", 0),
        ("stall_turns", 0),
    ],
)
def test_run_config_rejects_non_positive_budgets(
    tmp_path: Path, field: str, value: int
) -> None:
    values = {field: value}
    with pytest.raises(ValidationError):
        RunConfig.from_sources(workspace=tmp_path, topic="目标", **values)


def test_agent_profile_migrates_legacy_timeout_without_changing_boundary() -> None:
    legacy = AgentProfile.model_validate(
        {"command": "codex", "model": "gpt-5", "timeout_seconds": 1200}
    )

    assert legacy.hard_timeout_seconds == 1200
    assert legacy.effective_hard_timeout_seconds == 1200
    explicit = AgentProfile(command="codex", hard_timeout_seconds=4200)
    assert explicit.effective_hard_timeout_seconds == 4200


def test_run_config_validates_realtime_and_proxy_environment_settings(
    tmp_path: Path,
) -> None:
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        proxy_environment_allowlist=("HTTPS_PROXY", "SSL_CERT_FILE"),
    )

    assert config.proxy_environment_allowlist == (
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
    )
    with pytest.raises(ValidationError, match="代理或证书"):
        RunConfig(
            workspace=tmp_path,
            topic="目标",
            proxy_environment_allowlist=("DEEPSEEK_API_KEY",),
        )


def test_protocol_models_round_trip_as_json() -> None:
    session = AgentSessionState(
        session_id="session-1",
        generation=1,
        health=SessionHealth.ACTIVE,
        last_ack_commit="abc",
        last_update_id="UPD-1",
    )
    lease = LeaseState(
        run_id="RUN-1",
        turn_id="TURN-1",
        agent=AgentName.CODEX,
        base_commit="abc",
        goal_contract_version=1,
        issued_at="2026-07-16T10:00:00+08:00",
        status=LeaseStatus.ACTIVE,
    )
    turn = TurnResult(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit="abc",
        observed_head="abc",
        goal_contract_version=1,
        decision=Decision.REVISE,
        summary="完成第一轮修改",
        accepted_points=[],
        issues=[],
        requested_scope_additions=[],
        requested_validation_additions=[],
        completion_assessment=CompletionAssessment.INCOMPLETE,
    )
    state = DebateState(
        run_id="RUN-1",
        repo_root="/repo",
        target_branch="main",
        base_commit="abc",
        worktree_path="/state/worktree",
        debate_branch="debate/RUN-1",
        goal_contract_ref="goal-contract/v1.json",
        goal_contract_version=1,
        current_head="abc",
        current_turn=1,
        max_turns=8,
        next_writer=AgentName.CLAUDE,
        lease=lease,
        agent_sessions={AgentName.CODEX: session, AgentName.CLAUDE: session},
        pending_update_refs={AgentName.CODEX: [], AgentName.CLAUDE: []},
        issue_ledger_ref="issues.json",
        validation_plan_ref="validation-plan.json",
        ballots={},
        status=RunStatus.FAILED,
        failure_count=0,
        stall_count=0,
    )

    payload = json.loads(state.model_dump_json())

    assert payload["lease"]["agent"] == "codex"
    assert payload["next_writer"] == "claude"
    assert turn.model_dump(mode="json")["decision"] == "revise"


def test_handoff_and_final_models_bind_git_objects() -> None:
    summary = ChangeSummary(
        update_id="UPD-1",
        source_agent=AgentName.CODEX,
        turn_id="TURN-1",
        parent_commit="abc",
        commit_sha="def",
        goal_contract_version=1,
        changed_paths=["src/a.py"],
        behavior_summary=["新增行为"],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
    )
    update = PeerMemoryUpdate.from_change_summary(
        summary, target_agent=AgentName.CLAUDE
    )
    ack = ChangeAck(
        agent=AgentName.CLAUDE,
        commit_sha="def",
        reviewed_paths=[],
        understood_changes=["已理解"],
        risks_or_disputes=[],
        decision=Decision.ACCEPT,
    )
    ballot = Ballot(
        agent=AgentName.CLAUDE,
        commit_sha="def",
        goal_contract_version=1,
        completion=CompletionAssessment.COMPLETE,
        criteria=[{"criterion": "tests", "satisfied": True}],
        rationale="目标已完成",
    )
    tree_ack = TreeAck(
        agent=AgentName.CODEX,
        final_tree_sha="tree-1",
        accepted=True,
        rationale="代码和审计一致",
    )

    assert update.commit_sha == summary.commit_sha
    assert update.target_agent is AgentName.CLAUDE
    assert ack.commit_sha == ballot.commit_sha
    assert tree_ack.final_tree_sha == "tree-1"


def test_ballot_keeps_legacy_shape_and_requires_pair_when_opted_in() -> None:
    """验证旧 Ballot 兼容，反例字段必须成对且由运行时选项要求。"""
    ballot = Ballot(
        agent=AgentName.CLAUDE,
        commit_sha="def",
        goal_contract_version=1,
        completion=CompletionAssessment.COMPLETE,
        criteria=[{"criterion": "tests", "satisfied": True}],
        rationale="目标已完成",
    )

    assert ballot.strongest_counterexample is None
    with pytest.raises(ValueError, match="必须提供最强反例"):
        ballot.require_counterexample()
    with pytest.raises(ValidationError, match="必须同时提供"):
        Ballot(
            agent=AgentName.CLAUDE,
            commit_sha="def",
            goal_contract_version=1,
            completion=CompletionAssessment.INCOMPLETE,
            criteria=[],
            rationale="存在风险",
            strongest_counterexample="边界条件未覆盖",
        )
    ballot = ballot.model_copy(
        update={
            "strongest_counterexample": "边界条件未覆盖",
            "counterexample_resolution": "已补充边界测试并通过验证报告。",
        }
    )
    ballot.require_counterexample()



def test_agent_result_requires_structured_error_or_output() -> None:
    result = AgentResult(
        ok=True,
        agent=AgentName.CODEX,
        session_id="session-1",
        session_generation=1,
        turn_id="TURN-1",
        observed_head="abc",
        structured_output={"decision": "accept"},
        return_code=0,
        elapsed_seconds=1.2,
        raw_output_ref="raw/codex.jsonl",
    )
    assert result.error is None


def test_facilitator_decision_forbids_unknown_authority_fields() -> None:
    decision = FacilitatorDecision(
        decision_id="FAC-1",
        purpose=FacilitatorPurpose.TURN_ROUTING,
        head_sha="abc",
        goal_contract_version=2,
        mode=FacilitatorMode.NON_THINKING,
        action=FacilitatorAction.CONTINUE_DEBATE,
        recommended_next_writer=AgentName.CLAUDE,
        next_instruction="检查失败的验收条件",
        unresolved_criteria=["tests pass"],
        risk_flags=[],
        evidence_refs=["validation/TURN-1.json"],
    )

    assert decision.head_sha == "abc"
    with pytest.raises(ValidationError):
        FacilitatorDecision.model_validate(
            {**decision.model_dump(mode="json"), "mark_complete": True}
        )


def test_json_schemas_require_git_and_completion_fields() -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    turn_schema = json.loads((schema_root / "turn.schema.json").read_text())
    ack_schema = json.loads((schema_root / "change-ack.schema.json").read_text())
    ballot_schema = json.loads((schema_root / "ballot.schema.json").read_text())
    tree_schema = json.loads((schema_root / "tree-ack.schema.json").read_text())
    review_schema = json.loads((schema_root / "review.schema.json").read_text())

    assert {"base_commit", "observed_head", "completion_assessment"} <= set(
        turn_schema["required"]
    )
    assert "commit_sha" in ack_schema["required"]
    assert {"commit_sha", "completion"} <= set(ballot_schema["required"])
    counterexample_fields = {
        "strongest_counterexample",
        "counterexample_resolution",
    }
    assert counterexample_fields <= set(ballot_schema["required"])
    assert counterexample_fields <= set(ballot_schema["properties"])
    for field in counterexample_fields:
        assert {
            entry["type"] for entry in ballot_schema["properties"][field]["anyOf"]
        } == {
            "string",
            "null",
        }
    assert "final_tree_sha" in tree_schema["required"]
    assert {"head_sha", "protocol_version", "completion_assessment"} <= set(
        review_schema["required"]
    )


@pytest.mark.parametrize(
    ("schema_name", "model"),
    [
        ("turn.schema.json", TurnResult),
        ("change-ack.schema.json", ChangeAck),
        ("ballot.schema.json", Ballot),
        ("tree-ack.schema.json", TreeAck),
        ("review.schema.json", ReviewResult),
    ],
)
def test_json_schema_required_fields_match_protocol_models(
    schema_name: str, model: type[BaseModel]
) -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    schema = json.loads((schema_root / schema_name).read_text())
    model_schema = model.model_json_schema()

    required_fields = set(model_schema["required"])
    if schema_name == "ballot.schema.json":
        required_fields |= {
            "strongest_counterexample",
            "counterexample_resolution",
        }
    assert set(schema["required"]) == required_fields


def test_turn_schema_matches_issue_fields_and_protocol_enums() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1] / "schemas" / "turn.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    issue_schema = schema["properties"]["issues"]["items"]

    assert set(issue_schema["required"]) == set(issue_schema["properties"])
    assert schema["properties"]["agent"]["enum"] == [
        agent.value for agent in AgentName
    ]
    assert schema["properties"]["decision"]["enum"] == [
        decision.value for decision in Decision
    ]
    assert schema["properties"]["completion_assessment"]["enum"] == [
        assessment.value for assessment in CompletionAssessment
    ]
    validation_argv = schema["properties"]["requested_validation_additions"][
        "items"
    ]
    assert "已拆分 argv" in validation_argv["description"]
    assert validation_argv["items"]["minLength"] == 1


def test_all_runtime_schema_agent_enums_match_builtin_agents() -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    expected = [agent.value for agent in AgentName]

    for schema_name in (
        "turn.schema.json",
        "change-ack.schema.json",
        "ballot.schema.json",
        "tree-ack.schema.json",
        "review.schema.json",
    ):
        schema = json.loads((schema_root / schema_name).read_text())
        assert schema["properties"]["agent"]["enum"] == expected


def test_runtime_schemas_follow_strict_structured_output_object_rules() -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"

    def assert_strict_object(node: object, *, location: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            properties = node.get("properties", {})
            assert node.get("additionalProperties") is False, location
            assert set(node.get("required", [])) == set(properties), location
        for key, value in node.items():
            assert_strict_object(value, location=f"{location}.{key}")

    for path in sorted(schema_root.glob("*.schema.json")):
        assert_strict_object(json.loads(path.read_text()), location=path.name)


def test_wheel_configuration_includes_runtime_schemas() -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project_root / "pyproject.toml").read_text())

    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    assert force_include == {"schemas": "debate_agent/schemas"}
    assert (project_root / "schemas" / "review.schema.json").is_file()
    assert config["project"]["name"] == "debate-agent"
    assert config["project"]["scripts"] == {
        "debate-agent": "debate_agent.cli:entrypoint",
        "discuss-agent": "debate_agent.cli:entrypoint",
    }


def test_writer_turn_result_does_not_require_reviewer_fields() -> None:
    """验证 Writer 协议保持独立；无参数，无返回值。"""
    reviewer_only_fields = {
        "coverage",
        "findings",
        "strongest_counterexample",
        "counterexample_resolution",
        "protocol_version",
        "head_sha",
    }

    assert reviewer_only_fields.isdisjoint(TurnResult.model_fields)
