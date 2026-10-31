"""验证四维 Reviewer Prompt 的固定 fake-CLI 对照实验。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from git_helpers import git
from pydantic import BaseModel

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.facilitator import build_facilitator_decision_id
from debate_agent.git_workspace import GitWorkspace
from debate_agent.models import (
    AgentName,
    AgentResult,
    ChangeAck,
    CompletionAssessment,
    Decision,
    FacilitatorAction,
    FacilitatorContext,
    FacilitatorDecision,
    FacilitatorMode,
    FacilitatorPurpose,
    FacilitatorRecord,
    FacilitatorStatus,
    Issue,
    TurnResult,
)
from debate_agent.prompts import build_reviewer_prompt
from debate_agent.runtime import AgentBackend, DebateRuntime

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "adversarial_review_prompt"
REQUIRED_CONTROL_FIELDS = {
    "agent_combination",
    "agent_profiles",
    "initial_commit_sha",
    "goal_contract",
    "validation_commands",
    "max_turns",
    "timeout_seconds",
    "facilitator",
    "random_seed",
    "user_participation",
    "repetitions",
}
REQUIRED_LENSES = ("验收条件遗漏", "边界与反例", "集成兼容", "验证证据")


@dataclass(frozen=True)
class FakeCliRun:
    """保存经过 Runtime 调用链的 fake-CLI 审查输出与成本。"""

    result: TurnResult
    total_cli_calls: int
    review_rounds: int
    reviewer_prompt: str


class RuntimeFakeBackend(AgentBackend):
    """通过真实 Runtime 调用链返回固定任务的确定性结构化结果。"""

    def __init__(
        self, *, config: RunConfig, controls: dict[str, Any], task: dict[str, Any]
    ) -> None:
        """创建 fake 后端；参数为运行配置、控制变量和任务真值，返回实例。"""
        self.config = config
        self.controls = controls
        self.task = task
        self.random_seed = int(controls["random_seed"])
        self.calls: list[tuple[type[BaseModel], bool, str, str]] = []

    def _assert_frozen_controls(self, *, agent: AgentName, head: str) -> None:
        """核验后端收到的 Runtime 配置；参数为 Agent/HEAD，无返回值，偏离时抛错。"""
        expected_profile = self.controls["agent_profiles"][agent.value]
        configured_profile = self.config.profiles[agent]
        facilitator = self.controls["facilitator"]
        expected_participants = tuple(
            AgentName(value) for value in self.controls["agent_combination"]
        )
        assert head == self.controls["initial_commit_sha"]
        assert self.config.participants == expected_participants
        assert self.config.first_agent is expected_participants[0]
        assert self.config.max_turns == self.controls["max_turns"]
        assert self.config.validation_commands == tuple(
            tuple(command) for command in self.controls["validation_commands"]
        )
        assert self.random_seed == self.controls["random_seed"]
        assert configured_profile.model == expected_profile["model"]
        assert configured_profile.variant == expected_profile["variant"]
        assert expected_profile["reasoning"] == "off"
        assert self.config.facilitator_enabled is facilitator["enabled"]
        assert self.config.facilitator_model == facilitator["model"]
        assert self.config.facilitator_timeout_seconds == facilitator["timeout_seconds"]
        assert self.config.total_timeout_seconds == self.controls["timeout_seconds"]
        assert facilitator["variant"] == "deterministic"
        assert facilitator["reasoning"] == "off"

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
        """模拟 Runtime 的一次只读 CLI 调用。

        参数与 ``AgentBackend`` 一致；返回 ChangeAck 或既有 ``TurnResult``；调用
        不是只读、Prompt 与实验组不一致或模型类型异常时抛出 ``AssertionError``。
        """
        self.calls.append((response_model, read_only, schema_name, prompt))
        assert read_only is True
        head = git(worktree, "rev-parse", "HEAD")
        self._assert_frozen_controls(agent=agent, head=head)
        if response_model is ChangeAck:
            payload: BaseModel = ChangeAck(
                agent=agent,
                commit_sha=head,
                reviewed_paths=["README.md"],
                understood_changes=["已读取当前 HEAD"],
                risks_or_disputes=[],
                decision=Decision.ACCEPT,
            )
        elif response_model is TurnResult:
            response_key = (
                "four_lens" if "执行四维对抗审查" in prompt else "baseline"
            )
            response = self.task["fake_responses"][response_key]
            issues = [
                Issue(
                    id=f"{self.task['id']}-{index}",
                    severity=str(item["severity"]),
                    claim=str(item["claim"]),
                    evidence=[str(value) for value in item["evidence"]],
                    proposed_change=str(item["proposed_change"]),
                )
                for index, item in enumerate(response, start=1)
            ]
            complete = not any(issue.severity == "blocker" for issue in issues)
            payload = TurnResult(
                agent=agent,
                turn_id=turn_id,
                base_commit=head,
                observed_head=head,
                goal_contract_version=1,
                decision=Decision.ACCEPT if complete else Decision.REVISE,
                summary=f"fixed fake CLI: {self.task['id']}",
                accepted_points=[],
                issues=issues,
                requested_scope_additions=[],
                requested_validation_additions=[],
                completion_assessment=(
                    CompletionAssessment.COMPLETE
                    if complete
                    else CompletionAssessment.INCOMPLETE
                ),
            )
        else:
            raise AssertionError(f"unexpected response model: {response_model}")
        validated = response_model.model_validate(payload)
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            observed_head=getattr(validated, "observed_head", None),
            structured_output=validated.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{invocation_id}.json",
        )


class RuntimeFakeFacilitator:
    """经现有 Facilitator 节点验证冻结控制变量的只读 fake 服务。"""

    def __init__(self, *, config: RunConfig, controls: dict[str, Any]) -> None:
        """创建 fake Facilitator；参数为运行配置和控制变量，返回实例，不访问网络。"""
        self.config = config
        self.controls = controls
        self.random_seed = int(controls["random_seed"])
        self.contexts: list[FacilitatorContext] = []

    def evaluate(self, context: FacilitatorContext) -> FacilitatorRecord:
        """生成绑定当前事实的合法只读建议。

        参数为 Runtime 构造的 Facilitator 上下文；返回确定性决策记录；控制变量、绑定
        或用途不符合时抛出 ``AssertionError``。
        """
        facilitator = self.controls["facilitator"]
        participants = tuple(
            AgentName(value) for value in self.controls["agent_combination"]
        )
        assert self.config.participants == participants
        assert self.config.first_agent is participants[0]
        assert self.config.max_turns == self.controls["max_turns"]
        assert self.config.validation_commands == tuple(
            tuple(command) for command in self.controls["validation_commands"]
        )
        assert self.random_seed == self.controls["random_seed"]
        assert self.config.facilitator_enabled is facilitator["enabled"]
        assert self.config.facilitator_model == facilitator["model"]
        assert self.config.facilitator_timeout_seconds == facilitator["timeout_seconds"]
        assert facilitator["variant"] == "deterministic"
        assert facilitator["reasoning"] == "off"
        assert context.purpose is FacilitatorPurpose.TURN_ROUTING
        assert context.head_sha == self.controls["initial_commit_sha"]
        assert context.goal_contract_version == 1
        assert tuple(context.assessments) == participants
        self.contexts.append(context)
        decision = FacilitatorDecision(
            decision_id=build_facilitator_decision_id(context),
            purpose=context.purpose,
            head_sha=context.head_sha,
            goal_contract_version=context.goal_contract_version,
            mode=FacilitatorMode.NON_THINKING,
            action=FacilitatorAction.CONTINUE_DEBATE,
            recommended_next_writer=participants[0],
            next_instruction="只读检查当前 HEAD 后继续处理未完成验收。",
            unresolved_criteria=["固定 fake-CLI 基准尚未结束"],
            risk_flags=[],
            evidence_refs=[],
        )
        return FacilitatorRecord(
            decision_id=decision.decision_id,
            purpose=decision.purpose,
            head_sha=decision.head_sha,
            goal_contract_version=decision.goal_contract_version,
            mode=decision.mode,
            status=FacilitatorStatus.DECISION,
            decision=decision,
        )


class BaselinePromptRuntime(DebateRuntime):
    """仅在 Phase 1 对照组重建历史 Reviewer Prompt 的 Runtime 测试夹具。"""

    def _review_prompt(self, *, reviewer: AgentName, turn_id: str) -> str:
        """生成不含四维要求的历史 Prompt；参数同父类，返回只读审查文本。"""
        head = self._ensure_workspace().head()
        return build_reviewer_prompt(
            reviewer=reviewer.value,
            head_sha=head,
            goal_contract_json=self._contract().model_dump_json(indent=2),
            turn_id=turn_id,
            include_adversarial_lenses=False,
        )


class CorruptedFourLensPromptRuntime(DebateRuntime):
    """在测试中损坏四维 Prompt 标记，以证明 fake 后端只依据实际 Prompt 响应。"""

    def _review_prompt(self, *, reviewer: AgentName, turn_id: str) -> str:
        """删除四维标记；参数同父类，返回被损坏的只读审查文本。"""
        return super()._review_prompt(reviewer=reviewer, turn_id=turn_id).replace(
            "执行四维对抗审查", "四维审查标记已损坏"
        )


def _load_fixture(name: str) -> Any:
    """读取固定基准夹具；参数为文件名，返回 JSON 载荷，格式损坏时抛异常。"""
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _create_fixed_baseline_repo(
    *, root: Path, expected_head: str
) -> Path:
    """创建可重复的 Phase 1 固定 Git 基线。

    参数为根目录和夹具固定 HEAD；返回已初始化仓库；Git 初始化、提交或提交 SHA 与
    夹具不一致时抛出 ``CalledProcessError`` 或 ``AssertionError``。
    """
    repo = root / "fixed-baseline"
    repo.mkdir()
    base_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Benchmark Bot",
        "GIT_AUTHOR_EMAIL": "benchmark@example.test",
        "GIT_COMMITTER_NAME": "Benchmark Bot",
        "GIT_COMMITTER_EMAIL": "benchmark@example.test",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    }
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
        env=base_env,
    )
    (repo / "README.md").write_text("phase-1 baseline\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n", encoding="utf-8"
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_baseline.py").write_text(
        "def test_fixed_baseline_is_valid() -> None:\n"
        "    \"\"\"证明固定基线可通过与 Phase 2 相同的 pytest 验证。\"\"\"\n"
        "    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            ".gitignore",
            "README.md",
            "tests/test_baseline.py",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=base_env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "phase-1 fixed baseline"],
        check=True,
        capture_output=True,
        text=True,
        env=base_env,
    )
    assert git(repo, "rev-parse", "HEAD") == expected_head
    return repo


def _runtime_profiles(controls: dict[str, Any]) -> dict[AgentName, AgentProfile]:
    """将 Agent 控制变量构造成真实 RunConfig Profile；格式非法时抛出异常。"""
    return {
        AgentName(name): AgentProfile(
            command=f"fake-{name}",
            model=str(profile["model"]),
            variant=str(profile["variant"]),
        )
        for name, profile in controls["agent_profiles"].items()
    }


def _run_runtime_review(
    *,
    tmp_path: Path,
    baseline_repo: Path,
    prompt_label: str,
    runtime_class: type[DebateRuntime],
    task: dict[str, Any],
    ordinal: int,
) -> FakeCliRun:
    """经真实 Reviewer 节点执行一次固定任务。

    参数为临时目录、固定基线、Prompt 标签、Runtime 类型、任务和唯一序号；返回
    Runtime 产出的 Reviewer 结果及调用成本；运行时绑定、只读或结构化协议不满足时
    抛出异常。
    """
    controls = _load_fixture("controls.json")
    repo = tmp_path / f"{prompt_label}-{task['id']}-{ordinal}"
    subprocess.run(
        ["git", "clone", "--quiet", str(baseline_repo), str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert git(repo, "rev-parse", "HEAD") == controls["initial_commit_sha"]
    facilitator = controls["facilitator"]
    participants = tuple(AgentName(value) for value in controls["agent_combination"])
    config = RunConfig(
        workspace=repo,
        topic=str(controls["goal_contract"]["objective"]),
        state_root=tmp_path / "state",
        participants=participants,
        first_agent=participants[0],
        max_turns=int(controls["max_turns"]),
        total_timeout_seconds=int(controls["timeout_seconds"]),
        validation_commands=tuple(
            tuple(command) for command in controls["validation_commands"]
        ),
        allowed_scope=tuple(controls["goal_contract"]["allowed_scope"]),
        facilitator_enabled=bool(facilitator["enabled"]),
        facilitator_model=str(facilitator["model"]),
        facilitator_timeout_seconds=int(facilitator["timeout_seconds"]),
        profiles=_runtime_profiles(controls),
    )
    run_id = f"BENCH-{prompt_label}-{task['id']}-{ordinal}"
    artifacts = ArtifactStore.create(config, run_id=run_id)
    backend = RuntimeFakeBackend(config=config, controls=controls, task=task)
    fake_facilitator = RuntimeFakeFacilitator(config=config, controls=controls)
    runtime = runtime_class(
        config=config,
        run_id=run_id,
        artifacts=artifacts,
        backend=backend,
        facilitator=fake_facilitator,
    )
    runtime.workspace = GitWorkspace.preflight(
        repo,
        target_branch=config.target_branch,
        base_ref=config.target_branch,
        state_root=config.state_root,
        run_id=run_id,
        use_worktree=False,
        excluded_paths=config.protected_scope,
    )
    artifacts.write_repo(runtime.workspace.to_record())
    assert runtime.workspace.head() == controls["initial_commit_sha"]
    runtime._ensure_components()
    assert runtime.sessions is not None
    assert runtime.sessions.profiles == config.profiles
    assert runtime.config.participants == participants
    assert runtime.config.first_agent is participants[0]
    assert runtime.config.max_turns == controls["max_turns"]
    assert runtime.config.validation_commands == tuple(
        tuple(command) for command in controls["validation_commands"]
    )
    runtime.contract_store.create(
        objective=str(controls["goal_contract"]["objective"]),
        deliverables=controls["goal_contract"]["deliverables"],
        acceptance_criteria=controls["goal_contract"]["acceptance_criteria"],
        baseline_validation_commands=config.validation_commands,
        allowed_scope=config.allowed_scope,
        protected_scope=config.protected_scope,
        immutable_constraints=("fake benchmark",),
        command_policy=runtime.command_policy,
    )
    head = runtime.workspace.head()
    writer = TurnResult(
        agent=AgentName.CODEX,
        turn_id=f"TURN-{ordinal:03d}",
        base_commit=head,
        observed_head=head,
        goal_contract_version=1,
        decision=Decision.REVISE,
        summary="Writer checkpoint",
        accepted_points=[],
        issues=[],
        requested_scope_additions=[],
        requested_validation_additions=[],
        completion_assessment=CompletionAssessment.INCOMPLETE,
    )
    updated = runtime._node_review_current_head(
        {
            "turn_result": writer.model_dump(mode="json"),
            "made_checkpoint": True,
            "stall_count": 0,
            "scope_requests": {},
        }
    )
    result = TurnResult.model_validate(updated["review_result"])
    facilitator_update = runtime._node_facilitator_assess(
        {
            "turn_result": writer.model_dump(mode="json"),
            "review_result": result.model_dump(mode="json"),
            "assessments": {
                writer.agent.value: writer.completion_assessment.value,
                result.agent.value: result.completion_assessment.value,
            },
            "checkpoint_validation_passed": False,
            "stall_count": 0,
            "max_turns": config.max_turns,
            "current_turn": 1,
            "participants": [agent.value for agent in participants],
            "current_writer": writer.agent.value,
        }
    )
    assert len(fake_facilitator.contexts) == 1
    assert facilitator_update["facilitator_status"] == FacilitatorStatus.DECISION.value
    assert facilitator_update["facilitator_mode"] == FacilitatorMode.NON_THINKING.value
    reviewer_calls = [
        call for call in backend.calls if call[0] is TurnResult and call[1] is True
    ]
    assert len(reviewer_calls) == 1
    assert all(read_only for _model, read_only, _schema, _prompt in backend.calls)
    assert backend.calls[-1][2] == "turn.schema.json"
    return FakeCliRun(
        result=result,
        total_cli_calls=len(backend.calls),
        review_rounds=len(reviewer_calls),
        reviewer_prompt=reviewer_calls[0][3],
    )


def _run_profile(
    *,
    tmp_path: Path,
    baseline_repo: Path,
    prompt_label: str,
    runtime_class: type[DebateRuntime],
    tasks: list[dict[str, Any]],
) -> list[FakeCliRun]:
    """重复经 Runtime 执行固定 fake-CLI 基准。

    参数为临时目录、固定基线、Prompt 标签、Runtime 类型和任务集；返回按重复和
    任务排序的结果；任务夹具不完整或运行时调用失败时抛出异常。
    """
    controls = _load_fixture("controls.json")
    return [
        _run_runtime_review(
            tmp_path=tmp_path,
            baseline_repo=baseline_repo,
            prompt_label=prompt_label,
            runtime_class=runtime_class,
            task=task,
            ordinal=repeat * len(tasks) + index + 1,
        )
        for repeat in range(int(controls["repetitions"]))
        for index, task in enumerate(tasks)
    ]


def _supported_known_blocker(issue: Issue, task: dict[str, Any]) -> bool:
    """判断 Issue 是否与独立任务真值和证据相符；不匹配时返回 ``False``。"""
    truth = task["truth"]
    expected = truth["known_blocker"]
    return bool(
        issue.severity == "blocker"
        and expected
        and issue.claim == expected
        and set(truth["evidence"]).issubset(issue.evidence)
    )


def _metrics(runs: list[FakeCliRun], tasks: list[dict[str, Any]]) -> dict[str, float]:
    """从 Runtime 的 TurnResult 与独立真值聚合 Phase 1 指标。

    参数为重复运行结果和任务真值；返回质量与成本指标；任务集不完整、运行数不整齐
    或干净任务定位失败时抛出 ``AssertionError``。
    """
    repetitions = len(runs) // len(tasks)
    assert repetitions > 0
    assert len(runs) == repetitions * len(tasks)
    known_task_indexes = [
        index for index, task in enumerate(tasks) if task["truth"]["known_blocker"]
    ]
    known_discovered = sum(
        any(
            _supported_known_blocker(issue, tasks[index])
            for issue in run.result.issues
        )
        for repeat in range(repetitions)
        for index in known_task_indexes
        for run in [runs[repeat * len(tasks) + index]]
    )
    unsupported_blockers = sum(
        issue.severity == "blocker"
        and issue.claim not in task["truth"]["non_blocking_claims"]
        and not _supported_known_blocker(issue, task)
        for repeat in range(repetitions)
        for index, task in enumerate(tasks)
        for issue in runs[repeat * len(tasks) + index].result.issues
    )
    style_false_positives = sum(
        issue.severity == "blocker"
        and issue.claim in task["truth"]["non_blocking_claims"]
        for repeat in range(repetitions)
        for index, task in enumerate(tasks)
        for issue in runs[repeat * len(tasks) + index].result.issues
    )
    clean_indexes = [
        index for index, task in enumerate(tasks) if task["id"] == "clean-task"
    ]
    assert clean_indexes == [len(tasks) - 1]
    clean_runs = [
        runs[repeat * len(tasks) + clean_indexes[0]]
        for repeat in range(repetitions)
    ]
    return {
        "known_blocker_detection_rate": known_discovered
        / (len(known_task_indexes) * repetitions),
        "unsupported_or_style_errors": float(
            unsupported_blockers + style_false_positives
        ),
        "average_total_cli_calls": sum(run.total_cli_calls for run in runs)
        / repetitions,
        "average_review_rounds": sum(run.review_rounds for run in runs) / repetitions,
        "clean_completion_rate": sum(
            run.result.completion_assessment is CompletionAssessment.COMPLETE
            and not any(issue.severity == "blocker" for issue in run.result.issues)
            for run in clean_runs
        )
        / len(clean_runs),
        "clean_incomplete_without_blocker": float(
            sum(
                run.result.completion_assessment is CompletionAssessment.INCOMPLETE
                and not any(issue.severity == "blocker" for issue in run.result.issues)
                for run in clean_runs
            )
        ),
    }


def _phase_one_go(
    *,
    baseline: dict[str, float],
    four_lens: dict[str, float],
    baseline_runs: list[FakeCliRun],
    four_lens_runs: list[FakeCliRun],
    tasks: list[dict[str, Any]],
) -> bool:
    """执行冻结的 Phase 1 Go 判定；参数为结果与任务真值，返回是否全部通过。"""
    repetitions = len(baseline_runs) // len(tasks)
    paired_discovery_ok = all(
        sum(
            any(
                _supported_known_blocker(issue, tasks[index])
                for issue in run.result.issues
            )
            for index, run in enumerate(
                baseline_runs[offset : offset + len(tasks)]
            )
        )
        <= sum(
            any(
                _supported_known_blocker(issue, tasks[index])
                for issue in run.result.issues
            )
            for index, run in enumerate(
                four_lens_runs[offset : offset + len(tasks)]
            )
        )
        for offset in range(0, repetitions * len(tasks), len(tasks))
    )
    return bool(
        paired_discovery_ok
        and four_lens["known_blocker_detection_rate"]
        >= baseline["known_blocker_detection_rate"] + 0.10
        and four_lens["unsupported_or_style_errors"]
        <= baseline["unsupported_or_style_errors"]
        and four_lens["average_total_cli_calls"]
        <= baseline["average_total_cli_calls"] * 1.20
        and four_lens["average_review_rounds"]
        <= baseline["average_review_rounds"] * 1.20
        and four_lens["clean_incomplete_without_blocker"] == 0
        and four_lens["clean_completion_rate"] >= baseline["clean_completion_rate"]
    )


def test_prompt_benchmark_controls_and_four_lenses_are_frozen() -> None:
    """确保 Phase 1 控制变量完整，且四面 Prompt 强制完成性反例与完整阻断项。"""
    controls = _load_fixture("controls.json")

    assert set(controls) >= REQUIRED_CONTROL_FIELDS
    assert controls["repetitions"] == 3
    assert controls["user_participation"] is False
    assert controls["agent_combination"] == ["codex", "claude"]
    assert controls["max_turns"] == 8
    assert controls["validation_commands"] == [["python", "-m", "pytest", "-q"]]
    assert controls["random_seed"] == 20260724
    assert controls["initial_commit_sha"] == "a61c1c50c3c9cbd29cdbcc0d7764526a7a2ad5d8"
    assert controls["facilitator"]["timeout_seconds"] == 120
    assert {
        (profile["model"], profile["variant"], profile["reasoning"])
        for profile in controls["agent_profiles"].values()
    } == {
        ("fake-codex-1", "deterministic", "off"),
        ("fake-claude-1", "deterministic", "off"),
    }


def test_prompt_benchmark_uses_runtime_reviewer_path_and_meets_go_thresholds(
    tmp_path: Path,
) -> None:
    """经真实 Reviewer 节点验证三次配对运行、独立真值评分和 Phase 1 Go 阈值。"""
    controls = _load_fixture("controls.json")
    tasks = _load_fixture("tasks.json")
    baseline_repo = _create_fixed_baseline_repo(
        root=tmp_path, expected_head=str(controls["initial_commit_sha"])
    )
    baseline_runs = _run_profile(
        tmp_path=tmp_path,
        baseline_repo=baseline_repo,
        prompt_label="baseline",
        runtime_class=BaselinePromptRuntime,
        tasks=tasks,
    )
    four_lens_runs = _run_profile(
        tmp_path=tmp_path,
        baseline_repo=baseline_repo,
        prompt_label="four-lens",
        runtime_class=DebateRuntime,
        tasks=tasks,
    )
    baseline = _metrics(baseline_runs, tasks)
    four_lens = _metrics(four_lens_runs, tasks)
    expected_categories = {
        "遗漏验收条件",
        "边界与反例",
        "集成兼容",
        "验证证据",
        "无证据完成声明",
        "风格诱饵",
        "干净任务",
    }

    assert {task["category"] for task in tasks} == expected_categories
    assert len(baseline_runs) == len(four_lens_runs) == 3 * len(tasks)
    assert all("执行四维对抗审查" not in run.reviewer_prompt for run in baseline_runs)
    assert all(
        all(lens in run.reviewer_prompt for lens in REQUIRED_LENSES)
        for run in four_lens_runs
    )
    assert all(
        phrase in four_lens_runs[0].reviewer_prompt
        for phrase in (
            "最强的完成性反例",
            "一次性列出当前所有可识别的阻断项",
            "最小修改建议",
        )
    )
    assert baseline["average_total_cli_calls"] == four_lens[
        "average_total_cli_calls"
    ] == 14
    assert baseline["average_review_rounds"] == four_lens[
        "average_review_rounds"
    ] == 7
    assert _phase_one_go(
        baseline=baseline,
        four_lens=four_lens,
        baseline_runs=baseline_runs,
        four_lens_runs=four_lens_runs,
        tasks=tasks,
    )

    corrupted_run = _run_runtime_review(
        tmp_path=tmp_path,
        baseline_repo=baseline_repo,
        prompt_label="corrupted-four-lens",
        runtime_class=CorruptedFourLensPromptRuntime,
        task=tasks[0],
        ordinal=999,
    )
    assert "执行四维对抗审查" not in corrupted_run.reviewer_prompt
    assert corrupted_run.result.issues == []


def test_prompt_benchmark_rejects_output_without_truth_evidence(tmp_path: Path) -> None:
    """证明评分独立于 fake 输出：阻断项缺少真值证据时必须拒绝 Go。"""
    controls = _load_fixture("controls.json")
    tasks = _load_fixture("tasks.json")
    corrupted = copy.deepcopy(tasks)
    for task in corrupted:
        for finding in task["fake_responses"]["four_lens"]:
            if finding["severity"] == "blocker":
                finding["evidence"] = []
    baseline_repo = _create_fixed_baseline_repo(
        root=tmp_path, expected_head=str(controls["initial_commit_sha"])
    )
    baseline_runs = _run_profile(
        tmp_path=tmp_path,
        baseline_repo=baseline_repo,
        prompt_label="baseline",
        runtime_class=BaselinePromptRuntime,
        tasks=corrupted,
    )
    corrupted_runs = _run_profile(
        tmp_path=tmp_path,
        baseline_repo=baseline_repo,
        prompt_label="four-lens",
        runtime_class=DebateRuntime,
        tasks=corrupted,
    )
    baseline = _metrics(baseline_runs, corrupted)
    four_lens = _metrics(corrupted_runs, corrupted)

    assert four_lens["unsupported_or_style_errors"] > baseline[
        "unsupported_or_style_errors"
    ]
    assert not _phase_one_go(
        baseline=baseline,
        four_lens=four_lens,
        baseline_runs=baseline_runs,
        four_lens_runs=corrupted_runs,
        tasks=corrupted,
    )
