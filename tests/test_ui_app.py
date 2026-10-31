from __future__ import annotations

from pathlib import Path

from git_helpers import create_repo
from streamlit.testing.v1 import AppTest

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.invocation_events import InvocationEventStore, InvocationEventType
from debate_agent.models import AgentName, ReviewProtocolVersion, RunStatus
from debate_agent.ui.live_run_page import _phase_step_classes
from debate_agent.ui.view_models import build_profiles_from_form


def _app_path() -> Path:
    """返回 Streamlit 入口；无参数，返回页面文件路径。"""
    return (
        Path(__file__).parents[1]
        / "src"
        / "debate_agent"
        / "ui"
        / "app.py"
    )


def test_completed_phase_rail_marks_every_step_done() -> None:
    """验证运行完成后阶段轨道不再把末节点显示为进行中。"""
    completed = _phase_step_classes(
        "integration",
        status=RunStatus.COMPLETED.value,
    )
    running = _phase_step_classes(
        "integration",
        status=RunStatus.RUNNING.value,
    )

    assert completed == ("done",) * 7
    assert running == ("done",) * 6 + ("active",)


def test_streamlit_app_renders_safe_new_run_entry() -> None:
    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "Debate Control Center"
    assert any(button.label == "检查配置" for button in app.button)
    assert any(button.label == "开始讨论" for button in app.button)
    assert any(text_area.label == "任务目标" for text_area in app.text_area)
    worktree_toggle = next(
        toggle for toggle in app.toggle if toggle.label == "使用独立 Git worktree"
    )
    assert worktree_toggle.value is False
    integration_toggle = next(
        toggle for toggle in app.toggle if toggle.label == "完成后合并回目标分支"
    )
    assert integration_toggle.value is True
    unrestricted_toggle = next(
        toggle for toggle in app.toggle if toggle.label == "完全授权执行命令"
    )
    assert unrestricted_toggle.value is True
    validation = next(
        area for area in app.text_area if area.label == "验证命令"
    )
    assert validation.value == ""
    labels = {field.label for field in app.text_input}
    assert {
        "Codex CLI 路径",
        "Codex 模型",
        "Claude CLI 路径",
        "Claude 模型",
        "Qoder CLI 路径",
        "Qoder 模型",
        "OpenCode CLI 路径",
        "OpenCode 模型",
        "OpenCode Variant",
    } <= labels
    opencode_model = next(
        field for field in app.text_input if field.label == "OpenCode 模型"
    )
    assert opencode_model.value == "opencode/deepseek-v4-flash-free"
    participants = next(
        item for item in app.multiselect if item.label == "参与讨论的 CLI Agent"
    )
    assert participants.value == ["codex", "claude"]
    assert any(
        field.label == "启动超时（秒）" for field in app.number_input
    )
    assert any(field.label == "空闲超时（秒）" for field in app.number_input)
    assert any(field.label == "单次硬上限（秒）" for field in app.number_input)
    assert any(toggle.label == "显示 CLI 实时输出" for toggle in app.toggle)
    assert any(select.label == "MCP 模式" for select in app.selectbox)
    protocol_select = next(
        select for select in app.selectbox if select.label == "审查协议版本"
    )
    assert protocol_select.value == "review-v1"
    assert protocol_select.options == ["legacy-v0", "review-v1"]
    ledger_toggle = next(
        toggle for toggle in app.toggle if toggle.label == "启用 Issue Ledger"
    )
    assert ledger_toggle.value is False
    assert ledger_toggle.disabled is False
    counterexample_toggle = next(
        toggle for toggle in app.toggle if toggle.label == "启用反例式 Blind Ballot"
    )
    assert counterexample_toggle.value is False
    assert counterexample_toggle.disabled is False


def test_streamlit_counterexample_ballot_is_disabled_for_legacy_protocol() -> None:
    """验证页面不会在 legacy-v0 下提交反例终局投票选项。"""
    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()
    protocol_select = next(
        select for select in app.selectbox if select.label == "审查协议版本"
    )

    app = protocol_select.set_value("legacy-v0").run()

    counterexample_toggle = next(
        toggle for toggle in app.toggle if toggle.label == "启用反例式 Blind Ballot"
    )
    assert counterexample_toggle.value is False
    assert counterexample_toggle.disabled is True


def test_streamlit_opencode_variant_is_visible_only_for_catalog_definition() -> None:
    """验证页面仅为支持 variant 的 Catalog Agent 展示输入；无参数，无返回值。"""
    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()

    labels = {field.label for field in app.text_input}

    assert "OpenCode Variant" in labels
    assert "Codex Variant" not in labels
    assert "Claude Variant" not in labels
    assert "Qoder Variant" not in labels


def test_streamlit_form_profiles_preserve_catalog_optional_fields() -> None:
    """验证页面输入映射为等价 Profile；无参数，返回值为空。"""
    profiles = build_profiles_from_form(
        commands={agent: f"/opt/{agent.value}" for agent in AgentName},
        models={agent: f"{agent.value}-model" for agent in AgentName},
        variants={AgentName.OPENCODE: "quality"},
        native_profiles={
            AgentName.CODEX: "ignored",
            AgentName.OPENCLAW: "debate",
        },
        providers={
            AgentName.CODEX: "ignored",
            AgentName.HERMES: "deepseek",
        },
    )

    assert profiles[AgentName.OPENCODE] == AgentProfile(
        command="/opt/opencode",
        model="opencode-model",
        variant="quality",
    )
    assert all(
        profile.variant is None
        for agent, profile in profiles.items()
        if agent is not AgentName.OPENCODE
    )
    assert profiles[AgentName.CODEX].native_profile is None
    assert profiles[AgentName.CODEX].provider is None
    assert profiles[AgentName.OPENCLAW].native_profile == "debate"
    assert profiles[AgentName.HERMES].provider == "deepseek"


def test_streamlit_source_uses_catalog_driven_profiles_tracks_and_colors() -> None:
    """验证页面无固定三方配置分支；无参数和返回值，源码读取失败抛 OSError。"""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _app_path().parent.glob("*.py")
    )

    for fixed_fragment in (
        "default_codex_command",
        "default_claude_command",
        "default_qoder_command",
        "codex_command=config.profiles",
        "live.codex",
        "live.claude",
        "live.qoder",
        ".agent-lane.codex",
        ".agent-lane.claude",
        ".agent-lane.qoder",
    ):
        assert fixed_fragment not in source
    assert "agent_definitions()" in source
    assert "agent_definition(agent).ui_color" in source


def test_workspace_input_refreshes_current_branch_before_form_submit(
    tmp_path: Path,
) -> None:
    """验证输入工作空间后无需提交表单即可刷新并回填当前分支。"""
    repo = create_repo(tmp_path)
    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()
    workspace = next(
        field for field in app.text_input if field.label == "工作空间"
    )

    app = workspace.input(str(repo)).run()

    assert not app.exception
    assert any(caption.value == "当前分支：main" for caption in app.caption)
    target_branch = next(
        field for field in app.text_input if field.label == "目标分支覆盖"
    )
    assert target_branch.value == "main"


def test_first_agent_options_follow_current_participants() -> None:
    """验证首位 Agent 仅来自当前参与者；无参数和返回值，页面异常由测试报告。"""
    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()
    participants = next(
        item for item in app.multiselect if item.label == "参与讨论的 CLI Agent"
    )

    app = participants.set_value(["claude", "qoder"]).run()
    first_agent = next(
        item for item in app.selectbox if item.label == "首位 Writer"
    )

    assert first_agent.options == ["claude", "qoder"]


def test_monitor_renders_split_live_cli_events_as_safe_code(
    tmp_path: Path, monkeypatch
) -> None:
    """验证监控页分轨展示实时事件且模型 HTML 仅作为代码；参数为临时目录，无返回值。"""
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISCUSS_AGENT_STATE_ROOT", str(state_root))
    config = RunConfig(
        workspace=tmp_path,
        topic="展示实时输出",
        state_root=state_root,
    )
    ArtifactStore.create(config, run_id="RUN-UI-LIVE")
    event_store = InvocationEventStore(
        state_root=state_root,
        run_id="RUN-UI-LIVE",
    )
    event_store.publish(
        invocation_id="INV-C",
        agent=AgentName.CODEX,
        turn_id="turn-1",
        event_type=InvocationEventType.MESSAGE,
        title="Codex 正在分析",
        summary="<script>alert('unsafe')</script>",
        elapsed_seconds=1.5,
        idle_seconds=0.2,
        useful=True,
    )
    event_store.publish(
        invocation_id="INV-A",
        agent=AgentName.CLAUDE,
        turn_id="turn-2",
        event_type=InvocationEventType.TOOL_STARTED,
        title="Claude 执行测试",
        summary="python -m pytest -q",
        elapsed_seconds=2.0,
        idle_seconds=0.0,
        useful=True,
    )

    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()
    app = app.radio[0].set_value("运行监控").run()

    assert not app.exception
    assert {tab.label for tab in app.tabs} >= {
        "进度摘要",
        "Codex 实时",
        "Claude 实时",
        "原始输出",
        "验证与结论",
    }
    code_values = [block.value for block in app.code]
    assert any("<script>alert('unsafe')</script>" in value for value in code_values)
    markup = "\n".join(block.value for block in app.markdown)
    assert "Claude 执行测试" in markup
    assert "已参与非 Writer 调用 · 正在执行" in markup
    assert (
        '<div class="agent-name">claude</div>'
        '<div class="agent-action">等待首次调用</div>'
    ) not in markup


def test_monitor_renders_opencode_live_tab_lane_raw_tail_and_error_guidance(
    tmp_path: Path, monkeypatch
) -> None:
    """验证 OpenCode 监控视图动态渲染；参数为目录/环境，无返回值。"""
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISCUSS_AGENT_STATE_ROOT", str(state_root))
    config = RunConfig(
        workspace=tmp_path,
        topic="OpenCode 监控",
        state_root=state_root,
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.OPENCODE: AgentProfile(
                command="opencode", model="provider/model", variant="quality"
            ),
        },
    )
    store = ArtifactStore.create(config, run_id="RUN-UI-OPENCODE")
    store.write_status("failed", stop_reason="OpenCode 网络故障")
    (store.run_dir / "raw" / "INV-OPEN.stdout.log").write_text(
        "opencode raw tail\n", encoding="utf-8"
    )
    InvocationEventStore(
        state_root=state_root,
        run_id="RUN-UI-OPENCODE",
    ).publish(
        invocation_id="INV-OPEN",
        agent=AgentName.OPENCODE,
        turn_id="TURN-1",
        event_type=InvocationEventType.ERROR,
        title="OpenCode 模型连接失败",
        summary="network error",
        elapsed_seconds=1.0,
        idle_seconds=0.1,
        useful=False,
        terminal=True,
        termination_category="network_error",
    )

    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()
    app = app.radio[0].set_value("运行监控").run()

    assert not app.exception
    assert {tab.label for tab in app.tabs} >= {
        "Codex 实时",
        "OpenCode 实时",
        "原始输出",
    }
    markup = "\n".join(block.value for block in app.markdown)
    assert '<div class="agent-name">OpenCode</div>' in markup
    assert "OpenCode 模型连接失败" in markup
    assert any("opencode raw tail" in block.value for block in app.code)
    assert any(
        "CLI 登录状态、模型连通性" in message.value for message in app.info
    )


def test_monitor_rejects_semantically_invalid_issue_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    """验证 UI 经 Ledger 模型校验后才展示运行账本。"""
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISCUSS_AGENT_STATE_ROOT", str(state_root))
    config = RunConfig(
        workspace=tmp_path,
        topic="损坏 Ledger UI",
        state_root=state_root,
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
    )
    store = ArtifactStore.create(config, run_id="RUN-UI-LEDGER")
    store.write_issue_ledger_payload(
        {"protocol_version": "review-v1", "issues": [{"issue_id": "ISSUE-BAD"}]}
    )

    app = AppTest.from_file(str(_app_path()), default_timeout=10).run()
    app = app.radio[0].set_value("运行监控").run()

    assert not app.exception
    assert any("Issue Ledger 损坏" in message.value for message in app.error)
