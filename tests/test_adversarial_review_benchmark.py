"""验证发布前对抗审查基准、故障注入和多 Agent fake 矩阵。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import pytest
from git_helpers import create_repo, git
from pydantic import BaseModel

from debate_agent.adapters import AdapterOutputError
from debate_agent.artifacts import ArtifactStore
from debate_agent.cli_health import CliPreflightError, preflight_run_config
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.facilitator import FacilitatorDecisionError, _validate_decision
from debate_agent.models import (
    AgentName,
    AgentResult,
    Ballot,
    ChangeAck,
    CompletionAssessment,
    Decision,
    FacilitatorAction,
    FacilitatorContext,
    FacilitatorDecision,
    FacilitatorMode,
    FacilitatorPurpose,
    ReviewProtocolVersion,
    RunStatus,
    SessionHealth,
    TreeAck,
    TurnResult,
)
from debate_agent.prompts import build_structured_reviewer_prompt
from debate_agent.protocol_gateway import ProtocolBinding, ProtocolGateway
from debate_agent.review_benchmark import (
    BenchmarkControls,
    BenchmarkError,
    BenchmarkReport,
    BenchmarkRunSummary,
    write_final_benchmark_report,
    write_run_summary,
)
from debate_agent.review_models import (
    EvidenceKind,
    EvidenceRef,
    ReviewCoverage,
    ReviewCoverageStatus,
    ReviewDimension,
    ReviewResult,
)
from debate_agent.runtime import AgentBackend, DebateLifecycleService
from debate_agent.sessions import SessionManager

FIXTURE = (
    Path(__file__).parent / "fixtures" / "adversarial_review" / "final_benchmark.json"
)
REPORT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "superpowers"
    / "reports"
    / "adversarial-review-final-benchmark.md"
)
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def _fixture() -> dict[str, object]:
    """读取冻结最终基准夹具；无参数，返回 JSON 对象，损坏时抛 AssertionError。"""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _controls(
    *,
    payload: dict[str, object],
    protocol_version: str,
    participants: tuple[AgentName, ...],
) -> BenchmarkControls:
    """构造完全冻结的基准控制变量。

    参数为夹具、协议版本和参与者；返回控制模型；夹具字段缺失或类型错误时抛
    ``AssertionError``/``ValidationError``。
    """
    base = payload["base_controls"]
    profiles = payload["agent_profiles"]
    assert isinstance(base, dict)
    assert isinstance(profiles, dict)
    profile_payload = {agent.value: profiles[agent.value] for agent in participants}
    return BenchmarkControls.model_validate(
        {
            "protocol_version": protocol_version,
            "participants": [agent.value for agent in participants],
            "agent_profiles": profile_payload,
            "initial_commit_sha": base["initial_commit_sha"],
            "goal_contract": base["goal_contract"],
            "validation_commands": base["validation_commands"],
            "max_turns": base["max_turns"],
            "timeout_seconds": base["timeout_seconds"],
            "facilitator": base["facilitator"],
            "random_seed": base["random_seed"],
            "repetitions": base["repetitions"],
            "user_participation": base["user_participation"],
        }
    )


def _summary(
    *, group: str, run: dict[str, object], controls: BenchmarkControls
) -> BenchmarkRunSummary:
    """从固定响应构造单次公开摘要；参数为对照组、运行和控制，返回摘要。"""
    return BenchmarkRunSummary.model_validate(
        {
            "run_id": run["run_id"],
            "comparison_group": group,
            "controls": controls.model_dump(mode="json"),
            "audit_refs": ["validation/REVIEW-HEAD-fixed.json"],
            "quality": {
                "known_blockers_found": run["known_blockers_found"],
                "known_blockers_total": 5,
                "missing_acceptance_found": run["missing_acceptance_found"],
                "unsupported_completion_blockers": run[
                    "unsupported_completion_blockers"
                ],
                "style_false_positives": run["style_false_positives"],
                "repeated_findings": run["repeated_findings"],
                "ballot_overturns": run["ballot_overturns"],
                "clean_tasks_completed": 1,
                "clean_tasks_total": 1,
            },
            "cost": {
                "input_tokens": 0,
                "output_tokens": 0,
                "elapsed_seconds": 0.01,
                "turns": 2,
                "total_cli_calls": 2,
                "max_turns_reached": False,
                "stalled": False,
                "user_participation_events": 0,
            },
            "reliability": {
                "coverage_missing": 0,
                "invalid_evidence_rejected": 1 if group == "structured-v1" else 0,
                "output_repaired_or_degraded": 0,
                "session_context_lost": 0,
                "recovery_binding_errors": 0,
            },
            "terminal_status": "completed",
        }
    )


class MatrixBackend(AgentBackend):
    """以真实 Runtime 生命周期执行 2/3/4 Agent 的确定性 fake 组合。"""

    def __init__(self, *, state_root: Path, run_id: str) -> None:
        """创建 fake 后端；参数为状态根和 Run ID，无返回值。"""
        self.state_root = state_root
        self.run_id = run_id
        self.writer_count = 0
        self.calls: list[tuple[AgentName, str, bool]] = []

    def _validation_ref(self) -> str:
        """读取本 Run 当前 review 验证引用；无参数，返回相对路径，缺失时抛错。"""
        reports = sorted(
            path.relative_to(self.state_root / "runs" / self.run_id).as_posix()
            for path in (self.state_root / "runs" / self.run_id / "validation").glob(
                "REVIEW-HEAD-*.json"
            )
        )
        assert reports
        return reports[-1]

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
        """响应运行时调用。

        参数与 ``AgentBackend`` 一致；返回绑定当前 HEAD 的结构化结果；未知模型、
        读写权限错误或审计产物缺失时抛 AssertionError。
        """
        del schema_name, session_id, invocation_id
        self.calls.append((agent, response_model.__name__, read_only))
        head = git(worktree, "rev-parse", "HEAD")
        if response_model is TurnResult:
            assert read_only is False
            self.writer_count += 1
            (worktree / "result.txt").write_text(
                f"writer={agent.value}; count={self.writer_count}\n", encoding="utf-8"
            )
            payload: BaseModel = TurnResult(
                agent=agent,
                turn_id=turn_id,
                base_commit=head,
                observed_head=head,
                goal_contract_version=1,
                decision=Decision.ACCEPT,
                summary="固定 fake Writer checkpoint",
                accepted_points=[],
                issues=[],
                requested_scope_additions=[],
                requested_validation_additions=[],
                completion_assessment=CompletionAssessment.COMPLETE,
            )
        elif response_model is ChangeAck:
            assert read_only is True
            payload = ChangeAck(
                agent=agent,
                commit_sha=head,
                reviewed_paths=["result.txt"],
                understood_changes=["已读取当前 Writer checkpoint"],
                risks_or_disputes=[],
                decision=Decision.ACCEPT,
            )
        elif response_model is ReviewResult:
            assert read_only is True
            git_ref = EvidenceRef(
                kind=EvidenceKind.GIT_PATH,
                head_sha=head,
                artifact_ref=None,
                path="result.txt",
                locator=None,
            )
            validation_ref = EvidenceRef(
                kind=EvidenceKind.VALIDATION_REPORT,
                head_sha=head,
                artifact_ref=self._validation_ref(),
                path=None,
                locator=None,
            )
            payload = ReviewResult(
                agent=agent,
                turn_id=turn_id,
                head_sha=head,
                goal_contract_version=1,
                protocol_version=ReviewProtocolVersion.REVIEW_V1,
                coverage=[
                    ReviewCoverage(
                        dimension=dimension,
                        status=ReviewCoverageStatus.PASS,
                        summary="固定 fake 基准已覆盖",
                        evidence_refs=(
                            [validation_ref]
                            if dimension is ReviewDimension.VALIDATION
                            else [git_ref]
                        ),
                    )
                    for dimension in ReviewDimension
                ],
                findings=[],
                issue_dispositions=[],
                strongest_counterexample="无未解决反例",
                counterexample_resolution="当前 HEAD 的验证报告通过",
                completion_assessment=CompletionAssessment.COMPLETE,
                decision=Decision.ACCEPT,
                summary="固定 fake 结构化审查通过",
            )
        elif response_model is Ballot:
            assert read_only is True
            payload = Ballot(
                agent=agent,
                commit_sha=head,
                goal_contract_version=1,
                completion=CompletionAssessment.COMPLETE,
                criteria=[{"criterion": "fixed", "satisfied": True}],
                rationale="固定 fake 验收通过",
            )
        elif response_model is TreeAck:
            assert read_only is True
            tree = prompt.split("FINAL_TREE_SHA=", 1)[1].splitlines()[0]
            payload = TreeAck(
                agent=agent,
                final_tree_sha=tree,
                accepted=True,
                rationale="固定 fake final tree 已确认",
            )
        else:
            raise AssertionError(response_model)
        parsed = response_model.model_validate(payload)
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            observed_head=getattr(parsed, "observed_head", None),
            structured_output=parsed.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{agent.value}-{turn_id}.json",
        )


def test_final_benchmark_has_paired_controls_metrics_and_registered_summaries(
    tmp_path: Path,
) -> None:
    """验证三组各三次、全部指标、结构化 Run/Audit 摘要和可复核 Go 结论。"""
    payload = _fixture()
    groups = payload["groups"]
    paired = payload["paired_agent_combination"]
    assert isinstance(groups, dict)
    assert isinstance(paired, list)
    participants = tuple(AgentName(item) for item in paired)
    summaries: list[BenchmarkRunSummary] = []
    for group, group_payload in groups.items():
        assert isinstance(group, str)
        assert isinstance(group_payload, dict)
        protocol_version = group_payload["protocol_version"]
        runs = group_payload["runs"]
        assert isinstance(protocol_version, str)
        assert isinstance(runs, list)
        controls = _controls(
            payload=payload,
            protocol_version=protocol_version,
            participants=participants,
        )
        assert controls.repetitions == 3
        assert len(runs) == controls.repetitions
        summaries.extend(
            _summary(group=group, run=run, controls=controls)
            for run in runs
            if isinstance(run, dict)
        )
    assert len(summaries) == 9
    current = [item for item in summaries if item.comparison_group == "current-prompt"]
    four_lens = [item for item in summaries if item.comparison_group == "four-lens"]
    structured = [
        item for item in summaries if item.comparison_group == "structured-v1"
    ]
    assert len(current) == len(four_lens) == len(structured) == 3
    for baseline, four, v1 in zip(current, four_lens, structured, strict=True):
        for field in (
            "participants",
            "initial_commit_sha",
            "goal_contract",
            "validation_commands",
            "max_turns",
            "timeout_seconds",
            "facilitator",
            "random_seed",
            "user_participation",
        ):
            assert (
                getattr(baseline.controls, field)
                == getattr(four.controls, field)
                == getattr(v1.controls, field)
            )
        assert (
            v1.quality.known_blocker_detection_rate
            >= four.quality.known_blocker_detection_rate
        )
        assert v1.quality.clean_completion_rate == 1
        assert v1.quality.unsupported_completion_blockers == 0
        assert v1.quality.style_false_positives == 0
    config = RunConfig(
        workspace=tmp_path,
        topic="固定最终基准",
        state_root=tmp_path / "state",
        profiles={
            agent: AgentProfile(command=f"fake-{agent.value}", model="fake")
            for agent in AgentName
        },
    )
    artifacts = ArtifactStore.create(config, run_id="FINAL-BENCHMARK")
    for summary in summaries:
        reference = write_run_summary(
            artifacts=artifacts,
            summary=summary,
            head_sha=summary.controls.initial_commit_sha,
        )
        assert reference == f"benchmark/{summary.run_id}.json"
        artifacts.verify_supervisor_artifact(
            artifact_ref=reference,
            producer="review_benchmark",
            head_sha=summary.controls.initial_commit_sha,
        )
    report = BenchmarkReport(
        runs=tuple(summaries),
        phase_decisions={
            "Phase 1": "Go：四面 Prompt 达到冻结阈值。",
            "Phase 2": "Go：结构化协议未降低发现率。",
            "Ledger": "Go：重复 Finding 门禁已触发并已实施。",
            "Ballot": (
                "Go：两次过程 complete 后最终投回 incomplete 门禁已触发并已实施。"
            ),
        },
        real_cli_decisions={
            "Codex + Claude": "待真实 CLI 执行记录。",
            "Codex + Qoder": "待真实 CLI 执行记录。",
            "Claude + OpenCode": "待真实 CLI 执行记录。",
            "三 Agent": "待真实 CLI 执行记录。",
            "四 Agent": "待真实 CLI 执行记录。",
        },
    )
    temporary_report = tmp_path / "final-benchmark.md"
    write_final_benchmark_report(path=temporary_report, report=report)
    rendered = temporary_report.read_text(encoding="utf-8")
    for fragment in (
        "已知阻断",
        "遗漏验收",
        "无证据阻断",
        "风格误报",
        "Ballot 推翻",
        "Coverage 缺失",
        "非法 Evidence 拒绝",
        "Session 上下文丢失",
        "恢复绑定错误",
        "不保存隐藏推理",
    ):
        assert fragment in rendered
    with pytest.raises(BenchmarkError, match="不同摘要"):
        write_run_summary(
            artifacts=artifacts,
            summary=summaries[0].model_copy(update={"terminal_status": "failed"}),
            head_sha=summaries[0].controls.initial_commit_sha,
        )


@pytest.mark.parametrize(
    "participants",
    (
        (AgentName.CODEX, AgentName.CLAUDE),
        (AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER),
        (AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER, AgentName.OPENCODE),
    ),
)
def test_fake_lifecycle_matrix_completes_for_two_three_and_four_agents(
    tmp_path: Path, participants: tuple[AgentName, ...]
) -> None:
    """验证 2/3/4 Agent fake 生命周期始终单写、多方只读并完成终局门禁。"""
    matrix_root = tmp_path / f"matrix-{len(participants)}"
    matrix_root.mkdir()
    repo = create_repo(matrix_root)
    run_id = f"MATRIX-{len(participants)}"
    state_root = tmp_path / "state"
    profiles = {
        agent: AgentProfile(command="python", model=f"fake-{agent.value}")
        for agent in AgentName
    }
    config = RunConfig(
        workspace=repo,
        topic="在 result.txt 写入固定结果并通过 pytest 验证",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        state_root=state_root,
        participants=participants,
        first_agent=participants[0],
        validation_commands=(("python", "-c", "assert True"),),
        max_turns=len(participants) * 2 + 2,
        total_timeout_seconds=120,
        allowed_scope=("result.txt",),
        integrate_to_target=False,
        facilitator_enabled=False,
        profiles=profiles,
    )
    backend = MatrixBackend(state_root=state_root, run_id=run_id)
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend
    ).run_with_id(config, run_id=run_id)
    assert result["status"] == RunStatus.COMPLETED.value
    assert (repo / "result.txt").is_file()
    writer_calls = [call for call in backend.calls if call[1] == "TurnResult"]
    review_calls = [call for call in backend.calls if call[1] == "ReviewResult"]
    assert writer_calls
    assert review_calls
    assert all(read_only is False for _, _, read_only in writer_calls)
    assert all(read_only is True for _, _, read_only in review_calls)
    assert {
        agent
        for agent, response_name, _read_only in backend.calls
        if response_name == "Ballot"
    } == set(participants)


def test_final_benchmark_fault_injection_paths_are_explicit(tmp_path: Path) -> None:
    """验证截断、证据、越权、Session、恢复、HEAD 与不可用 CLI 的故障分类可定位。"""
    gateway = ProtocolGateway()
    with pytest.raises(AdapterOutputError, match="结构化输出无效"):
        gateway._decode_payload(
            '{"agent":"codex"',
            response_model=TurnResult,
            binding=ProtocolBinding(
                agent=AgentName.CODEX,
                turn_id="TURN-1",
                head_sha="a" * 40,
                goal_contract_version=1,
            ),
            normalizer_text=None,
        )
    context = FacilitatorContext(
        run_id="FAULT-1",
        purpose=FacilitatorPurpose.TURN_ROUTING,
        turn_id_or_revision="TURN-1",
        head_sha="a" * 40,
        goal_contract_version=1,
        objective="fault",
        deliverables=["result.txt"],
        acceptance_criteria=["pass"],
        effective_scope=["result.txt"],
        validation_passed=True,
        assessments={AgentName.CODEX: CompletionAssessment.INCOMPLETE},
        stall_count=0,
        remaining_turns=1,
    )
    illegal = FacilitatorDecision(
        decision_id="FAC-1",
        purpose=FacilitatorPurpose.TURN_ROUTING,
        head_sha="a" * 40,
        goal_contract_version=1,
        mode=FacilitatorMode.NON_THINKING,
        action=FacilitatorAction.REGENERATE_AUDIT,
        next_instruction="越权关闭问题",
    )
    with pytest.raises(FacilitatorDecisionError, match="policy_rejection"):
        _validate_decision(
            illegal,
            context=context,
            decision_id="FAC-1",
            mode=FacilitatorMode.NON_THINKING,
        )
    profiles = {
        AgentName.QODER: AgentProfile(command="missing-debate-agent-cli"),
        AgentName.CLAUDE: AgentProfile(command="missing-debate-agent-cli"),
    }
    unavailable = RunConfig(
        workspace=tmp_path,
        topic="CLI 不可用故障注入",
        state_root=tmp_path / "unavailable-state",
        participants=(AgentName.QODER, AgentName.CLAUDE),
        first_agent=AgentName.QODER,
        profiles=profiles,
        facilitator_enabled=False,
    )
    with pytest.raises(CliPreflightError, match=r"qoder CLI 预检失败 \[executable\]"):
        preflight_run_config(unavailable)
    manager = SessionManager.open(
        state_root=tmp_path / "session-state",
        run_id="FAULT-SESSION",
        workspace=tmp_path,
        profiles={
            AgentName.CODEX: AgentProfile(command="fake-codex", model="fake"),
            AgentName.CLAUDE: AgentProfile(command="fake-claude", model="fake"),
        },
    )
    manager.bind(AgentName.CLAUDE, "lost-session")
    rebuilt = manager.rebuild(AgentName.CLAUDE, health=SessionHealth.LOST)
    assert rebuilt.generation == 2
    assert rebuilt.session_id is None
    assert rebuilt.health is SessionHealth.NEW


def test_final_benchmark_report_is_generated_from_frozen_data() -> None:
    """验证提交的最终报告保留全部阶段与真实 CLI 阻塞条件。"""
    content = REPORT.read_text(encoding="utf-8")
    for fragment in (
        "三组对照",
        "三次",
        "2 / 3 / 4 Agent",
        "Structured Output 截断",
        "Reviewer 写入",
        "Facilitator 越权",
        "Session 丢失",
        "HEAD 变化",
        "CLI 不可用",
        "Codex + Claude",
        "Codex + Qoder",
        "Claude + OpenCode",
        "三 Agent",
        "四 Agent",
    ):
        assert fragment in content


def test_structured_reviewer_prompt_explains_supervisor_validation_artifact() -> None:
    """验证 Reviewer 明确把 state 内验证报告当作可直接引用的权威证据。"""
    report_ref = "validation/REVIEW-HEAD-TURN-001-deadbeef.json"
    prompt = build_structured_reviewer_prompt(
        reviewer="claude",
        head_sha="a" * 40,
        goal_contract_json="{}",
        turn_id="REVIEW-TURN-001",
        goal_contract_version=1,
        validation_report_ref=report_ref,
    )
    assert f"CURRENT_VALIDATION_REPORT={report_ref}" in prompt
    assert "可能不在 Git 树或工作空间内" in prompt
    assert "逐字作为 kind=validation_report 的 artifact_ref" in prompt
    assert "自由文本伪装成 test_result/artifact_ref" in prompt
    assert (
        "本提示没有提供任何已登记的test_result 引用，因此不得返回 kind=test_result"
        in prompt
    )
    assert "git_diff 只能引用当前 HEAD相对本 Run 冻结基线实际修改的路径" in prompt
    assert "不能用 git_diff 证明某文件未变化" in prompt
