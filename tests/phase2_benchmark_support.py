"""Phase 2 基准测试的仓库、运行与断言辅助函数。"""

import json
import os
import subprocess
import sys
from pathlib import Path

from git_helpers import git
from phase2_benchmark_backends import (
    FIXTURE_ROOT as FIXTURE_ROOT,
)
from phase2_benchmark_backends import (
    PHASE_ONE_FIXTURE_ROOT as PHASE_ONE_FIXTURE_ROOT,
)
from phase2_benchmark_backends import (
    PHASE_ONE_REPORT as PHASE_ONE_REPORT,
)
from phase2_benchmark_backends import (
    REPORT as REPORT,
)
from phase2_benchmark_backends import (
    REQUIRED_CONTROLS as REQUIRED_CONTROLS,
)
from phase2_benchmark_backends import (
    REQUIRED_GOAL_CONTRACT_FIELDS as REQUIRED_GOAL_CONTRACT_FIELDS,
)
from phase2_benchmark_backends import (
    FixedReviewBackend as FixedReviewBackend,
)
from phase2_benchmark_backends import (
    LifecycleBenchmarkBackend as LifecycleBenchmarkBackend,
)
from phase2_benchmark_backends import (
    ResponseModel as ResponseModel,
)

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.git_workspace import GitWorkspace
from debate_agent.goal_contract import GoalContract
from debate_agent.graph import SQLiteCheckpoint
from debate_agent.models import (
    AgentName,
    Ballot,
    CompletionAssessment,
    Decision,
    ReviewProtocolVersion,
    TurnResult,
)
from debate_agent.review_models import (
    ReviewResult,
)
from debate_agent.runtime import DebateLifecycleService, DebateRuntime


def _load(name: str) -> object:
    """读取固定 Phase 2 夹具；参数为文件名，返回 JSON 载荷，损坏时抛异常。"""
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))

def _load_phase_one(name: str) -> object:
    """读取冻结的 Phase 1 夹具；参数为文件名，返回 JSON 载荷，损坏时抛异常。"""
    return json.loads((PHASE_ONE_FIXTURE_ROOT / name).read_text(encoding="utf-8"))

def _assert_phase_one_pairing(controls: dict[str, object]) -> dict[str, object]:
    """校验 Phase 2 与通过的 Phase 1 固定输入完全配对。

    参数为 Phase 2 控制变量；返回已读取的 Phase 1 任务集。夹具、报告、控制变量或
    GoalContract 任一漂移时抛 ``AssertionError``，避免把不同实验误作质量对照。
    """
    phase_one_controls = _load_phase_one("controls.json")
    phase_one_tasks = _load_phase_one("tasks.json")
    assert isinstance(phase_one_controls, dict)
    assert isinstance(phase_one_tasks, list)
    phase_one_report = PHASE_ONE_REPORT.read_text(encoding="utf-8")
    assert "**判定：Go。**" in phase_one_report
    assert "三次固定 fake-CLI 配对运行" in phase_one_report
    for field in (
        "agent_combination",
        "agent_profiles",
        "initial_commit_sha",
        "validation_commands",
        "max_turns",
        "timeout_seconds",
        "facilitator",
        "random_seed",
        "repetitions",
    ):
        assert controls[field] == phase_one_controls[field]
    assert phase_one_controls["user_participation"] is False
    assert controls["user_participation"] == "none"
    phase_one_goal = phase_one_controls["goal_contract"]
    goal_contract = controls["goal_contract"]
    assert isinstance(phase_one_goal, dict)
    assert isinstance(goal_contract, dict)
    assert set(goal_contract) == REQUIRED_GOAL_CONTRACT_FIELDS
    expected_goal_contract = {
        "version": phase_one_goal["version"],
        "objective": phase_one_goal["objective"],
        "deliverables": phase_one_goal["deliverables"],
        "acceptance_criteria": phase_one_goal["acceptance_criteria"],
        "baseline_validation_commands": controls["validation_commands"],
        "added_validation_commands": [],
        "allowed_scope": phase_one_goal["allowed_scope"],
        "protected_scope": [".git", ".agent/debate", ".agent/discuss", ".env"],
        "derived_scope": [],
        "immutable_constraints": ["fake benchmark"],
    }
    assert goal_contract == expected_goal_contract
    return {"tasks": phase_one_tasks}

def _assert_goal_contract_controls(
    *, contract: GoalContract, controls: dict[str, object]
) -> None:
    """核对实际持久化 GoalContract 的全部字段均来自固定控制变量。

    参数为创建后的契约和 Phase 2 控制变量；无返回值；任何字段被省略、默认替换或
    未进入 Reviewer Prompt 时抛 ``AssertionError``。
    """
    expected = controls["goal_contract"]
    assert isinstance(expected, dict)
    assert contract.model_dump(mode="json") == expected

def _phase_one_four_lens_quality(
    tasks: list[object],
) -> dict[str, float]:
    """从通过的 Phase 1 固定夹具计算四面 Prompt 质量指标。

    参数为 Phase 1 任务集；返回可与单次 Phase 2 配对运行比较的指标。评分同时核对
    独立真值的 Claim 与 Evidence，避免信任 fake-CLI 自报结果；夹具格式错误时抛
    ``AssertionError``。
    """
    known_detected = 0
    missing_detected = 0
    unsupported_or_style_errors = 0
    clean_completed = 0
    known_total = 0
    for task in tasks:
        assert isinstance(task, dict)
        truth = task["truth"]
        responses = task["fake_responses"]["four_lens"]
        assert isinstance(truth, dict)
        assert isinstance(responses, list)
        expected_claim = truth["known_blocker"]
        blockers = [
            response for response in responses if response["severity"] == "blocker"
        ]
        supported = any(
            expected_claim
            and response["claim"] == expected_claim
            and set(truth["evidence"]).issubset(response["evidence"])
            for response in blockers
        )
        if expected_claim:
            known_total += 1
            known_detected += int(supported)
        if task["id"] == "missing-acceptance":
            missing_detected += int(supported)
        unsupported_or_style_errors += sum(
            not (
                expected_claim
                and response["claim"] == expected_claim
                and set(truth["evidence"]).issubset(response["evidence"])
            )
            for response in blockers
        )
        if task["id"] == "clean-task":
            clean_completed = int(not blockers)
    assert known_total > 0
    return {
        "known_blocker_detection_rate": known_detected / known_total,
        "missing_acceptance_detection_rate": float(missing_detected),
        "unsupported_or_style_errors": float(unsupported_or_style_errors),
        "clean_completion_rate": float(clean_completed),
    }

def _phase_two_quality(
    *, tasks: list[dict[str, object]], reviews: list[ReviewResult]
) -> dict[str, float]:
    """从实际持久化的 Phase 2 Review 计算单次配对质量指标。

    参数为与 Phase 1 顺序相同的任务和当前重复的真实 Review；返回质量指标；缺少
    对应任务、错误 Finding 绑定或运行数不一致时抛 ``AssertionError``。
    """
    assert len(tasks) == len(reviews)
    known_tasks = [task for task in tasks if task["blocking"]]
    known_detected = 0
    missing_detected = 0
    unsupported_or_style_errors = 0
    clean_completed = 0
    for task, review in zip(tasks, reviews, strict=True):
        expected_blocking = bool(task["blocking"])
        blocking_findings = [finding for finding in review.findings if finding.blocking]
        supported = any(
            str(task["id"]) in finding.claim
            and finding.finding_id == f"FIND-{task['fingerprint']}"
            for finding in blocking_findings
        )
        if expected_blocking:
            known_detected += int(supported)
        else:
            unsupported_or_style_errors += len(blocking_findings)
        if task["id"] == "missing-acceptance":
            missing_detected = int(supported)
        if task["id"] == "clean-task":
            clean_completed = int(
                review.completion_assessment is CompletionAssessment.COMPLETE
                and not blocking_findings
            )
    assert known_tasks
    return {
        "known_blocker_detection_rate": known_detected / len(known_tasks),
        "missing_acceptance_detection_rate": float(missing_detected),
        "unsupported_or_style_errors": float(unsupported_or_style_errors),
        "clean_completion_rate": float(clean_completed),
    }

def _assert_quality_not_worse_than_phase_one(
    *, phase_one: dict[str, float], phase_two: dict[str, float]
) -> None:
    """执行单次 Phase 2 对 Phase 1 的机械质量下限比较。

    参数为同一固定任务顺序的一对质量指标；无返回值；发现率下降、误报增加或干净
    任务完成率下降时抛 ``AssertionError``。
    """
    assert (
        phase_two["known_blocker_detection_rate"]
        >= phase_one["known_blocker_detection_rate"]
    )
    assert (
        phase_two["missing_acceptance_detection_rate"]
        >= phase_one["missing_acceptance_detection_rate"]
    )
    assert (
        phase_two["unsupported_or_style_errors"]
        <= phase_one["unsupported_or_style_errors"]
    )
    assert phase_two["clean_completion_rate"] >= phase_one["clean_completion_rate"]

def _create_fixed_baseline_repo(*, root: Path, expected_head: str) -> Path:
    """创建与 Phase 1 完全相同的确定性 Git 基线。

    参数为临时根和夹具声明的初始 HEAD；返回固定仓库；Git 初始化、提交或 SHA 与
    夹具不一致时抛 ``CalledProcessError`` 或 ``AssertionError``。
    """
    repo = root / "fixed-baseline"
    repo.mkdir()
    environment = {
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
        env=environment,
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
        env=environment,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "phase-1 fixed baseline"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert git(repo, "rev-parse", "HEAD") == expected_head
    return repo

def _clone_fixed_baseline(*, baseline_repo: Path, root: Path) -> Path:
    """克隆固定基线以隔离一次 Phase 2 运行。

    参数为已校验的固定基线和目标根；返回克隆仓库；克隆失败时抛
    ``CalledProcessError``。
    """
    repo = root / "repo"
    subprocess.run(
        ["git", "clone", "--quiet", str(baseline_repo), str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo

def _assert_applied_controls(
    *, config: RunConfig, controls: dict[str, object]
) -> None:
    """验证 Phase 2 全部冻结控制变量已进入真实运行配置。

    参数为运行配置和固定控制变量；无返回值；任何生命周期输入偏离时抛
    ``AssertionError``。随机种子与用户参与是 fake 基准 Harness 控制项，必须分别
    为固定数值与无人参与，不能被错误地当作 RunConfig 未支持字段静默遗漏。
    """
    facilitator = controls["facilitator"]
    profiles_control = controls["agent_profiles"]
    assert isinstance(facilitator, dict)
    assert isinstance(profiles_control, dict)
    participants = tuple(AgentName(item) for item in controls["agent_combination"])
    assert config.protocol_version is ReviewProtocolVersion.REVIEW_V1
    assert config.participants == participants
    assert config.first_agent is participants[0]
    assert config.max_turns == controls["max_turns"]
    assert config.total_timeout_seconds == controls["timeout_seconds"]
    assert config.validation_commands == tuple(
        tuple(command) for command in controls["validation_commands"]
    )
    assert config.facilitator_enabled is facilitator["enabled"]
    assert config.facilitator_model == facilitator["model"]
    assert config.facilitator_timeout_seconds == facilitator["timeout_seconds"]
    assert facilitator["variant"] == "deterministic"
    assert facilitator["reasoning"] == "off"
    for agent in participants:
        expected = profiles_control[agent.value]
        profile = config.profiles[agent]
        assert profile.model == expected["model"]
        assert profile.variant == expected["variant"]
        assert expected["reasoning"] == "off"
    assert controls["random_seed"] == 20260724
    assert controls["user_participation"] == "none"

def _persisted_ballot_rounds(artifacts: ArtifactStore) -> list[dict[AgentName, Ballot]]:
    """从实际 SQLite checkpoint 读取每次完成的 Ballot 序列。

    参数为完成或继续中的 run 产物；返回按发生顺序的投票回合。每次新 Ballot 进入状态
    前，前一持久化快照必须已经记录全员过程 ``complete`` 与最终验证通过；否则抛
    ``AssertionError``。成功 run 会清理 invocation journal，因此本函数故意以长期保留
    的图 checkpoint 而非内存或已清理的原始 CLI 输出为唯一事实来源。
    """
    checkpoint_path = artifacts.run_dir / "checkpoint.sqlite"
    snapshots: list[dict[str, object]] = []
    with SQLiteCheckpoint.open(checkpoint_path) as checkpoint:
        records = list(
            checkpoint.saver.list(
                {"configurable": {"thread_id": artifacts.run_id}}
            )
        )
    for record in reversed(records):
        values = record.checkpoint.get("channel_values", {})
        assert isinstance(values, dict)
        snapshots.append(values)
    rounds: list[dict[AgentName, Ballot]] = []
    previous: dict[str, object] | None = None
    previous_marker: str | None = None
    for values in snapshots:
        ballots = values.get("ballots")
        if not isinstance(ballots, dict) or not ballots:
            previous = values
            continue
        marker = json.dumps(ballots, ensure_ascii=False, sort_keys=True)
        if marker == previous_marker:
            previous = values
            continue
        assert previous is not None
        assessments = previous.get("assessments")
        assert isinstance(assessments, dict)
        assert set(assessments) == {AgentName.CODEX.value, AgentName.CLAUDE.value}
        assert all(
            value == CompletionAssessment.COMPLETE.value
            for value in assessments.values()
        )
        assert previous.get("final_validation_passed") is True
        round_ballots = {
            AgentName(agent): Ballot.model_validate(payload)
            for agent, payload in ballots.items()
        }
        assert set(round_ballots) == {AgentName.CODEX, AgentName.CLAUDE}
        rounds.append(round_ballots)
        previous_marker = marker
        previous = values
    return rounds

def _run_fixed_review(
    *,
    tmp_path: Path,
    baseline_repo: Path,
    ordinal: int,
    task: dict[str, object] | None = None,
    mutate_review: bool = False,
) -> tuple[ReviewResult, ArtifactStore, FixedReviewBackend, DebateRuntime]:
    """创建独立提交后经真实 Runtime 执行一次 review-v1。

    参数为临时根、序号和只读故障开关；返回实际持久化 Review、产物 Store 和 fake
    backend。仓库/协议/证据门禁失败时抛异常。
    """
    controls = _load("controls.json")
    assert isinstance(controls, dict)
    _assert_phase_one_pairing(controls)
    root = tmp_path / f"run-{ordinal}"
    root.mkdir()
    repo = _clone_fixed_baseline(baseline_repo=baseline_repo, root=root)
    assert git(repo, "rev-parse", "HEAD") == controls["initial_commit_sha"]
    (repo / "result.txt").write_text("checkpoint\n", encoding="utf-8")
    git(repo, "add", "result.txt")
    git(repo, "commit", "-m", "fixed writer checkpoint")
    profiles = {
        AgentName.CODEX: AgentProfile(
            command="fake-codex", model="fake-codex-1", variant="deterministic"
        ),
        AgentName.CLAUDE: AgentProfile(
            command="fake-claude", model="fake-claude-1", variant="deterministic"
        ),
    }
    facilitator = controls["facilitator"]
    assert isinstance(facilitator, dict)
    config = RunConfig(
        workspace=repo,
        topic=str(controls["goal_contract"]["objective"]),
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        state_root=root / "state",
        validation_commands=tuple(
            tuple(command) for command in controls["validation_commands"]
        ),
        max_turns=int(controls["max_turns"]),
        allowed_scope=tuple(controls["goal_contract"]["allowed_scope"]),
        facilitator_enabled=bool(facilitator["enabled"]),
        facilitator_model=str(facilitator["model"]),
        facilitator_timeout_seconds=int(facilitator["timeout_seconds"]),
        profiles=profiles,
    )
    _assert_applied_controls(config=config, controls=controls)
    run_id = f"PHASE2-{ordinal}"
    artifacts = ArtifactStore.create(config, run_id=run_id)
    backend = FixedReviewBackend(
        controls=controls, task=task, mutate_review=mutate_review
    )
    runtime = DebateRuntime(
        config=config, run_id=run_id, artifacts=artifacts, backend=backend
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
    runtime._ensure_components()
    contract = runtime.contract_store.create(
        objective=str(controls["goal_contract"]["objective"]),
        deliverables=controls["goal_contract"]["deliverables"],
        acceptance_criteria=controls["goal_contract"]["acceptance_criteria"],
        baseline_validation_commands=controls["goal_contract"][
            "baseline_validation_commands"
        ],
        allowed_scope=controls["goal_contract"]["allowed_scope"],
        protected_scope=controls["goal_contract"]["protected_scope"],
        immutable_constraints=controls["goal_contract"]["immutable_constraints"],
        command_policy=runtime.command_policy,
    )
    _assert_goal_contract_controls(contract=contract, controls=controls)
    head = runtime.workspace.head()
    writer = TurnResult(
        agent=AgentName.CODEX,
        turn_id=f"TURN-{ordinal:03d}",
        base_commit=head,
        observed_head=head,
        goal_contract_version=1,
        decision=Decision.ACCEPT,
        summary="固定 Writer checkpoint",
        accepted_points=[],
        issues=[],
        requested_scope_additions=[],
        requested_validation_additions=[],
        completion_assessment=CompletionAssessment.COMPLETE,
    )
    state = runtime._node_review_current_head(
        {
            "turn_result": writer.model_dump(mode="json"),
            "made_checkpoint": True,
            "stall_count": 0,
            "scope_requests": {},
        }
    )
    review = ReviewResult.model_validate(state["review_result"])
    return review, artifacts, backend, runtime

def _run_full_lifecycle(
    *, tmp_path: Path, baseline_repo: Path, ordinal: int
) -> tuple[dict[str, object], LifecycleBenchmarkBackend, ArtifactStore]:
    """经完整 graph/final/ballot 路径运行一个固定 review-v1 基准。

    参数为临时根和运行序号；返回终态与记录全部调用的 fake backend。预检、协议、
    最终验证、Ballot 或 TreeAck 失败时抛异常。
    """
    controls = _load("controls.json")
    assert isinstance(controls, dict)
    _assert_phase_one_pairing(controls)
    root = tmp_path / f"lifecycle-{ordinal}"
    root.mkdir()
    repo = _clone_fixed_baseline(baseline_repo=baseline_repo, root=root)
    assert git(repo, "rev-parse", "HEAD") == controls["initial_commit_sha"]
    profiles = {
        AgentName.CODEX: AgentProfile(
            command="python", model="fake-codex-1", variant="deterministic"
        ),
        AgentName.CLAUDE: AgentProfile(
            command="python", model="fake-claude-1", variant="deterministic"
        ),
    }
    run_id = f"PHASE2-LIFECYCLE-{ordinal}"
    backend = LifecycleBenchmarkBackend(
        controls=controls,
        state_root=root / "state",
        run_id=run_id,
        reject_first_ballot=ordinal <= 2,
    )
    facilitator = controls["facilitator"]
    assert isinstance(facilitator, dict)
    config = RunConfig(
        workspace=repo,
        topic=str(controls["goal_contract"]["objective"]),
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        state_root=root / "state",
        validation_commands=tuple(
            tuple(command) for command in controls["validation_commands"]
        ),
        max_turns=int(controls["max_turns"]),
        total_timeout_seconds=int(controls["timeout_seconds"]),
        allowed_scope=tuple(controls["goal_contract"]["allowed_scope"]),
        integrate_to_target=False,
        facilitator_enabled=bool(facilitator["enabled"]),
        facilitator_model=str(facilitator["model"]),
        facilitator_timeout_seconds=int(facilitator["timeout_seconds"]),
        profiles=profiles,
    )
    _assert_applied_controls(config=config, controls=controls)
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend
    ).run_with_id(config, run_id=run_id)
    artifacts = ArtifactStore.open(state_root=config.state_root, run_id=run_id)
    return result, backend, artifacts

def _build_final_validation_runtime(
    *,
    tmp_path: Path,
    baseline_repo: Path,
    label: str,
    validation_commands: tuple[tuple[str, ...], ...] | None = None,
) -> tuple[DebateRuntime, ArtifactStore, GitWorkspace]:
    """准备会在最终验证阶段修改工作树并提交的受控 Runtime。

    参数为临时根和固定基线；返回 Runtime、产物 Store 与工作区；预检或契约构造
    失败时抛异常。该辅助只用于证明 final validation 会拒绝命令副作用。
    """
    controls = _load("controls.json")
    assert isinstance(controls, dict)
    _assert_phase_one_pairing(controls)
    root = tmp_path / f"final-validation-race-{label}"
    root.mkdir()
    repo = _clone_fixed_baseline(baseline_repo=baseline_repo, root=root)
    assert git(repo, "rev-parse", "HEAD") == controls["initial_commit_sha"]
    mutation_command = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import subprocess; "
            "Path('result.txt').write_text('tampered\\n', encoding='utf-8'); "
            "subprocess.run(['git', 'add', 'result.txt'], check=True); "
            "subprocess.run(['git', '-c', 'user.name=Benchmark Bot', "
            "'-c', 'user.email=benchmark@example.test', 'commit', '-m', "
            "'tampered final validation'], check=True)"
        ),
    )
    commands = validation_commands or (mutation_command,)
    profiles = {
        AgentName.CODEX: AgentProfile(
            command="fake-codex", model="fake-codex-1", variant="deterministic"
        ),
        AgentName.CLAUDE: AgentProfile(
            command="fake-claude", model="fake-claude-1", variant="deterministic"
        ),
    }
    facilitator = controls["facilitator"]
    assert isinstance(facilitator, dict)
    config = RunConfig(
        workspace=repo,
        topic=str(controls["goal_contract"]["objective"]),
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        state_root=root / "state",
        validation_commands=commands,
        max_turns=int(controls["max_turns"]),
        total_timeout_seconds=int(controls["timeout_seconds"]),
        allowed_scope=tuple(controls["goal_contract"]["allowed_scope"]),
        facilitator_enabled=bool(facilitator["enabled"]),
        facilitator_model=str(facilitator["model"]),
        facilitator_timeout_seconds=int(facilitator["timeout_seconds"]),
        profiles=profiles,
    )
    run_id = "PHASE2-FINAL-VALIDATION-RACE"
    artifacts = ArtifactStore.create(config, run_id=run_id)
    runtime = DebateRuntime(
        config=config,
        run_id=run_id,
        artifacts=artifacts,
        backend=FixedReviewBackend(controls=controls),
    )
    workspace = GitWorkspace.preflight(
        repo,
        target_branch=config.target_branch,
        base_ref=config.target_branch,
        state_root=config.state_root,
        run_id=run_id,
        use_worktree=False,
        excluded_paths=config.protected_scope,
    )
    runtime.workspace = workspace
    artifacts.write_repo(workspace.to_record())
    runtime._ensure_components()
    runtime.contract_store.create(
        objective=config.topic,
        deliverables=("result.txt",),
        acceptance_criteria=("当前 HEAD 验证通过",),
        baseline_validation_commands=config.validation_commands,
        allowed_scope=config.allowed_scope,
        protected_scope=config.protected_scope,
        immutable_constraints=("final validation race benchmark",),
        command_policy=runtime.command_policy,
    )
    return runtime, artifacts, workspace
