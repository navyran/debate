from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import create_repo

from debate_agent.agent_catalog import agent_definition
from debate_agent.artifacts import ArtifactStore
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.events import RunEventStore
from debate_agent.invocation_events import (
    InvocationEventStore,
    InvocationEventType,
    Redactor,
)
from debate_agent.models import AgentName, ReviewProtocolVersion
from debate_agent.ui.view_models import (
    RunDraft,
    inspect_workspace,
    list_run_summaries,
    parse_command,
    read_invocation_live_view,
    read_raw_output_tails,
)


def test_parse_command_preserves_quoted_argument_without_shell() -> None:
    assert parse_command("python -m pytest 'tests/unit test.py' -q") == (
        "python",
        "-m",
        "pytest",
        "tests/unit test.py",
        "-q",
    )
    with pytest.raises(ValueError, match="命令不能为空"):
        parse_command("   ")


def test_run_draft_builds_safe_config_with_simple_inputs(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    draft = RunDraft(
        workspace=repo,
        topic="修复登录超时并补充测试",
        allowed_paths=("src", "tests"),
        validation_command_lines=("python -m pytest -q",),
        state_root=tmp_path / "state",
        max_turns=6,
        use_worktree=False,
    )

    config = draft.to_config()

    assert config.target_branch == "main"
    assert config.allowed_scope == ("src", "tests")
    assert config.validation_commands == (("python", "-m", "pytest", "-q"),)
    assert config.max_turns == 6
    assert config.use_worktree is False
    assert config.integrate_to_target is True
    assert config.protocol_version is ReviewProtocolVersion.REVIEW_V1
    assert config.unrestricted_commands is True
    assert set(config.profiles) == set(AgentName)
    assert {
        "codex_command",
        "codex_model",
        "claude_command",
        "claude_model",
        "qoder_command",
        "qoder_model",
    }.isdisjoint(RunDraft.model_fields)


def test_run_draft_can_preserve_result_branch_without_target_integration(
    tmp_path: Path,
) -> None:
    """验证页面可关闭目标集成；参数为临时目录，无返回值。"""
    repo = create_repo(tmp_path)
    config = RunDraft(
        workspace=repo,
        topic="先生成候选，后续继续修改",
        state_root=tmp_path / "state",
        integrate_to_target=False,
    ).to_config()

    assert config.integrate_to_target is False


def test_run_draft_preserves_explicit_legacy_protocol_version(tmp_path: Path) -> None:
    """验证页面草稿传递协议版本；参数为临时目录，无返回值。"""
    repo = create_repo(tmp_path)

    config = RunDraft(
        workspace=repo,
        topic="恢复旧协议任务",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.LEGACY_V0,
    ).to_config()

    assert config.protocol_version is ReviewProtocolVersion.LEGACY_V0


def test_run_draft_freezes_optional_issue_ledger_only_for_review_v1(
    tmp_path: Path,
) -> None:
    """验证 UI 草稿默认关闭且显式冻结 Ledger；参数为临时目录，无返回值。"""
    repo = create_repo(tmp_path)
    config = RunDraft(
        workspace=repo,
        topic="记录重复问题",
        state_root=tmp_path / "state",
        issue_ledger_enabled=True,
    ).to_config()

    assert config.issue_ledger_enabled is True
    with pytest.raises(ValueError, match="仅支持 review-v1"):
        RunDraft(
            workspace=repo,
            topic="旧协议账本",
            state_root=tmp_path / "legacy",
            protocol_version=ReviewProtocolVersion.LEGACY_V0,
            issue_ledger_enabled=True,
        ).to_config()


def test_run_draft_freezes_optional_counterexample_ballot_only_for_review_v1(
    tmp_path: Path,
) -> None:
    """验证页面草稿默认关闭且仅允许 review-v1 显式启用反例投票。"""
    repo = create_repo(tmp_path)
    config = RunDraft(
        workspace=repo,
        topic="记录最终反例",
        state_root=tmp_path / "state",
        counterexample_ballot=True,
    ).to_config()

    assert config.counterexample_ballot is True
    with pytest.raises(ValueError, match="反例式 Blind Ballot 仅支持 review-v1"):
        RunDraft(
            workspace=repo,
            topic="旧协议反例投票",
            state_root=tmp_path / "legacy",
            protocol_version=ReviewProtocolVersion.LEGACY_V0,
            counterexample_ballot=True,
        ).to_config()


def test_run_draft_selects_qoder_and_freezes_its_profile(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    config = RunDraft(
        workspace=repo,
        topic="Codex 与 Qoder 讨论",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.QODER),
        first_agent=AgentName.QODER,
        profiles={
            AgentName.CODEX: AgentProfile(command="/opt/codex"),
            AgentName.CLAUDE: AgentProfile(command="/opt/claude"),
            AgentName.QODER: AgentProfile(command="/opt/qodercli"),
            AgentName.OPENCODE: AgentProfile(command="/opt/opencode"),
        },
    ).to_config()

    assert config.participants == (AgentName.CODEX, AgentName.QODER)
    assert config.first_agent is AgentName.QODER
    assert config.profiles[AgentName.QODER].command == "/opt/qodercli"


def test_run_draft_accepts_legacy_profiles_when_participants_are_covered(
    tmp_path: Path,
) -> None:
    """验证旧三方 Profile 可运行默认参与者；参数为临时目录，无返回值。"""
    repo = create_repo(tmp_path)
    config = RunDraft(
        workspace=repo,
        topic="继续历史三方运行",
        state_root=tmp_path / "state",
        profiles={
            AgentName.CODEX: AgentProfile(command="/opt/codex"),
            AgentName.CLAUDE: AgentProfile(command="/opt/claude"),
            AgentName.QODER: AgentProfile(command="/opt/qodercli"),
        },
    ).to_config()

    assert set(config.profiles) == {
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
    }


def test_run_draft_rejects_selected_opencode_without_profile(tmp_path: Path) -> None:
    """验证缺少已选 OpenCode Profile 会失败；参数为临时目录，无返回值。"""
    repo = create_repo(tmp_path)
    draft = RunDraft(
        workspace=repo,
        topic="选择 OpenCode",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(command="/opt/codex"),
            AgentName.CLAUDE: AgentProfile(command="/opt/claude"),
            AgentName.QODER: AgentProfile(command="/opt/qodercli"),
        },
    )

    with pytest.raises(ValueError, match="OpenCode.*Profile"):
        draft.to_config()


def test_run_draft_freezes_cli_models_layered_timeouts_mcp_and_proxy_names(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    config = RunDraft(
        workspace=repo,
        topic="长时间任务",
        state_root=tmp_path / "state",
        profiles={
            AgentName.CODEX: AgentProfile(
                command="/opt/codex", model="gpt-test"
            ),
            AgentName.CLAUDE: AgentProfile(
                command="/opt/claude", model="sonnet"
            ),
            AgentName.QODER: AgentProfile(command="/opt/qodercli"),
            AgentName.OPENCODE: AgentProfile(command="/opt/opencode"),
        },
        startup_timeout_seconds=240,
        idle_timeout_seconds=180,
        hard_timeout_seconds=4200,
        heartbeat_interval_seconds=4,
        max_process_failures=2,
        mcp_mode="full",
        proxy_environment_allowlist=("HTTPS_PROXY", "SSL_CERT_FILE"),
        realtime_output_enabled=True,
        raw_output_tail_lines=800,
    ).to_config()

    assert config.profiles[AgentName.CODEX].command == "/opt/codex"
    assert config.profiles[AgentName.CODEX].model == "gpt-test"
    assert config.profiles[AgentName.CLAUDE].command == "/opt/claude"
    assert config.profiles[AgentName.CLAUDE].model == "sonnet"
    assert config.profiles[AgentName.CODEX].startup_timeout_seconds == 240
    assert config.profiles[AgentName.CODEX].idle_timeout_seconds == 180
    assert config.profiles[AgentName.CODEX].hard_timeout_seconds == 4200
    assert config.profiles[AgentName.CODEX].heartbeat_interval_seconds == 4
    assert config.mcp_mode == "full"
    assert config.proxy_environment_allowlist == (
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
    )
    assert config.raw_output_tail_lines == 800


def test_workspace_inspection_reports_cleanliness_cli_and_facilitator(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    available = lambda command: f"/usr/bin/{command}"  # noqa: E731

    clean = inspect_workspace(
        repo,
        expected_branch="main",
        environment={},
        which=available,
    )

    assert clean.can_start is True
    assert clean.branch == "main"
    assert clean.by_key("deepseek").level == "warning"
    (repo / "untracked.txt").write_text("dirty", encoding="utf-8")

    dirty = inspect_workspace(
        repo,
        expected_branch="main",
        environment={"DEEPSEEK_API_KEY": "configured"},
        which=available,
    )

    assert dirty.can_start is False
    assert dirty.by_key("git_clean").level == "error"
    assert dirty.by_key("deepseek").level == "success"


@pytest.mark.parametrize(
    "participants",
    (
        (AgentName.CODEX, AgentName.CLAUDE),
        (AgentName.CODEX, AgentName.QODER),
        (AgentName.CLAUDE, AgentName.QODER),
        tuple(AgentName),
    ),
)
def test_workspace_inspection_only_checks_selected_catalog_profiles(
    tmp_path: Path, participants: tuple[AgentName, ...]
) -> None:
    """验证预检只检查参与者；参数为临时目录，返回值为空，Git 异常转为检查项。"""
    repo = create_repo(tmp_path)
    queried: list[str] = []
    profiles = {
        AgentName.CODEX: AgentProfile(command="codex-custom"),
        AgentName.CLAUDE: AgentProfile(command="claude-custom"),
        AgentName.QODER: AgentProfile(command="qoder-custom"),
        AgentName.OPENCODE: AgentProfile(command="opencode-custom"),
        AgentName.OPENCLAW: AgentProfile(command="openclaw-custom"),
        AgentName.HERMES: AgentProfile(command="hermes-custom"),
        AgentName.CODEBUDDY: AgentProfile(command="codebuddy-custom"),
    }

    inspection = inspect_workspace(
        repo,
        profiles=profiles,
        participants=participants,
        environment={},
        which=lambda command: queried.append(command) or f"/bin/{command}",
    )

    assert queried == [profiles[agent].command for agent in participants]
    for agent in AgentName:
        if agent in participants:
            assert inspection.by_key(agent.value).label == (
                f"{agent_definition(agent).display_name} CLI"
            )
        else:
            with pytest.raises(StopIteration):
                inspection.by_key(agent.value)


def test_workspace_inspection_allows_automatic_git_initialization(
    tmp_path: Path,
) -> None:
    """验证页面允许未初始化目录启动；参数为临时目录，无返回值。"""
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "README.md").write_text("draft\n", encoding="utf-8")

    inspection = inspect_workspace(
        workspace,
        expected_branch="master",
        environment={},
        which=lambda command: f"/usr/bin/{command}",
    )

    assert inspection.can_start is True
    assert inspection.branch == "master"
    assert inspection.by_key("git_repo").level == "warning"
    assert inspection.by_key("git_clean").level == "warning"


def test_list_run_summaries_uses_status_config_and_progress(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    config = RunConfig(
        workspace=tmp_path,
        topic="第一个任务",
        state_root=state_root,
    )
    first = ArtifactStore.create(config, run_id="RUN-1")
    first.write_status("blocked", stop_reason="等待恢复")
    RunEventStore(state_root=state_root, run_id="RUN-1").publish(
        kind="node_started",
        phase="validation",
        title="执行验证",
        current_turn=2,
        max_turns=8,
        status="blocked",
    )
    ArtifactStore.create(
        config.model_copy(update={"topic": "第二个任务"}), run_id="RUN-2"
    )

    summaries = list_run_summaries(state_root)

    assert [item.run_id for item in summaries] == ["RUN-2", "RUN-1"]
    assert summaries[1].topic == "第一个任务"
    assert summaries[1].phase == "validation"
    assert summaries[1].current_turn == 2
    assert summaries[1].stop_reason == "等待恢复"


def test_invocation_live_view_reads_incrementally_and_splits_agent_tracks(
    tmp_path: Path,
) -> None:
    """验证调用事件按游标增量读取并按 Agent 分轨；参数为临时目录，无返回值。"""
    state_root = tmp_path / "state"
    store = InvocationEventStore(state_root=state_root, run_id="RUN-LIVE")
    first = store.publish(
        invocation_id="INV-CODEX",
        agent=AgentName.CODEX,
        turn_id="turn-1",
        event_type=InvocationEventType.TOOL_STARTED,
        title="执行 pytest",
        summary="python -m pytest -q",
        elapsed_seconds=2.0,
        idle_seconds=0.0,
        useful=True,
    )
    store.publish(
        invocation_id="INV-CLAUDE",
        agent=AgentName.CLAUDE,
        turn_id="turn-2",
        event_type=InvocationEventType.FILE_CHANGED,
        title="更新测试",
        changed_paths=("tests/test_demo.py",),
        elapsed_seconds=3.0,
        idle_seconds=0.5,
        useful=True,
    )

    initial = read_invocation_live_view(
        state_root=state_root,
        run_id="RUN-LIVE",
        after_seq=0,
        limit=1000,
    )
    incremental = read_invocation_live_view(
        state_root=state_root,
        run_id="RUN-LIVE",
        after_seq=first.seq,
        limit=1000,
    )

    assert initial.last_seq == 2
    assert [event.agent for event in initial.events] == [
        AgentName.CODEX,
        AgentName.CLAUDE,
    ]
    assert tuple(initial.tracks) == tuple(AgentName)
    assert initial.tracks[AgentName.CODEX].event_count == 1
    assert initial.tracks[AgentName.CODEX].last_title == "执行 pytest"
    assert initial.tracks[AgentName.CLAUDE].changed_file_count == 1
    assert "codex" not in type(initial).model_fields
    assert "claude" not in type(initial).model_fields
    assert "qoder" not in type(initial).model_fields
    assert [event.seq for event in incremental.events] == [2]
    assert incremental.tracks[AgentName.CODEX].events == ()
    assert [
        event.seq for event in incremental.tracks[AgentName.CLAUDE].events
    ] == [2]


def test_invocation_live_view_bounds_large_history_and_degrades_on_partial_tail(
    tmp_path: Path,
) -> None:
    """验证大事件流受窗口限制且半行安全降级；参数为临时目录，无返回值。"""
    state_root = tmp_path / "state"
    store = InvocationEventStore(state_root=state_root, run_id="RUN-MANY")
    for index in range(1005):
        store.publish(
            invocation_id="INV-MANY",
            agent=AgentName.CODEX,
            turn_id="turn-1",
            event_type=InvocationEventType.HEARTBEAT,
            title=f"heartbeat-{index}",
            elapsed_seconds=float(index),
            idle_seconds=float(index),
            useful=False,
        )
    with store.events_path.open("ab") as handle:
        handle.write(b'{"seq":1006')

    view = read_invocation_live_view(
        state_root=state_root,
        run_id="RUN-MANY",
        after_seq=0,
        limit=1000,
    )

    assert len(view.events) == 1000
    assert view.events[0].seq == 6
    assert view.events[-1].seq == 1005
    assert view.last_seq == 1005
    assert view.warning is None


def test_raw_output_tail_is_bounded_redacted_and_missing_files_are_safe(
    tmp_path: Path,
) -> None:
    """验证 raw 尾读限制、脱敏和缺失降级；参数为临时目录，无返回值。"""
    state_root = tmp_path / "state"
    raw_dir = state_root / "runs" / "RUN-RAW" / "raw"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "INV-1.stdout.log"
    raw_path.write_text(
        "old line\n"
        "<script>alert('x')</script>\n"
        "API_KEY=top-secret\n"
        "session=123e4567-e89b-12d3-a456-426614174000\n",
        encoding="utf-8",
    )

    tails = read_raw_output_tails(
        state_root=state_root,
        run_id="RUN-RAW",
        redactor=Redactor(
            workspace=tmp_path,
            secret_values=("top-secret",),
        ),
        lines=3,
    )
    missing = read_raw_output_tails(
        state_root=state_root,
        run_id="RUN-MISSING",
        redactor=Redactor(workspace=tmp_path),
        lines=3,
    )

    assert len(tails) == 1
    assert tails[0].file_name == "INV-1.stdout.log"
    assert "old line" not in tails[0].text
    assert "top-secret" not in tails[0].text
    assert "123e4567-e89b-12d3-a456-426614174000" not in tails[0].text
    assert "<script>alert('x')</script>" in tails[0].text
    assert missing == ()
