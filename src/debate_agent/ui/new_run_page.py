"""Debate Control Center 的新建讨论页面。"""

# ruff: noqa: E501

from __future__ import annotations

import html
import json
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Literal, cast

import streamlit as st

from debate_agent.agent_catalog import (
    ModelPolicy,
    agent_definition,
    agent_definitions,
)
from debate_agent.cli_health import CLI_DEFAULT_MODEL, discover_cli_model
from debate_agent.config import AgentProfile, infer_target_branch
from debate_agent.launcher import RunLauncher
from debate_agent.models import AgentName, ReviewProtocolVersion
from debate_agent.ui.constants import DEFAULT_OPENCODE_MODEL, default_state_root
from debate_agent.ui.view_models import (
    RunDraft,
    WorkspaceInspection,
    build_profiles_from_form,
    detect_branch,
    inspect_workspace,
)


def _split_lines(value: str) -> tuple[str, ...]:
    """拆分非空文本行；参数为多行文本，返回去空格元组，不抛异常。"""
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _split_paths(value: str) -> tuple[str, ...]:
    """拆分逗号或换行路径；参数为文本，返回路径元组，空值默认仓库根。"""
    normalized = value.replace(",", "\n")
    paths = _split_lines(normalized)
    return paths or (".",)


def _render_inspection(report: WorkspaceInspection) -> None:
    """展示启动预检；参数为检查报告，无返回值，不执行外部动作。"""
    icons = {"success": "●", "warning": "◆", "error": "■"}
    rows = []
    for check in report.checks:
        level = html.escape(check.level)
        rows.append(
            '<div class="check-row">'
            f'<span class="check-{level}">{icons.get(check.level, "•")}</span>'
            f"<strong>{html.escape(check.label)}</strong>"
            f"<span>{html.escape(check.detail)}</span></div>"
        )
    st.markdown("".join(rows), unsafe_allow_html=True)
    if report.can_start:
        st.success("预检通过，可以启动讨论。")
    else:
        st.error("存在阻断项。请按上方提示修复后再启动。")


def _draft_from_form(
    *,
    workspace: str,
    topic: str,
    protocol_version: str,
    issue_ledger_enabled: bool,
    counterexample_ballot: bool,
    target_branch: str,
    use_worktree: bool,
    integrate_to_target: bool,
    allowed_paths: str,
    validation_commands: str,
    setup_commands: str,
    participants: tuple[str, ...],
    first_agent: str,
    max_turns: int,
    total_timeout_minutes: int,
    max_process_failures: int,
    profiles: dict[AgentName, AgentProfile],
    startup_timeout_seconds: int,
    idle_timeout_seconds: int,
    hard_timeout_seconds: int,
    heartbeat_interval_seconds: int,
    realtime_output_enabled: bool,
    raw_output_tail_lines: int,
    mcp_mode: Literal["minimal", "full"],
    proxy_environment_allowlist: tuple[str, ...],
    facilitator_enabled: bool,
    facilitator_model: str,
    allow_network: bool,
    unrestricted_commands: bool,
) -> RunDraft:
    """构建页面草稿；参数为表单值，返回 RunDraft，字段非法时抛错。"""
    return RunDraft(
        workspace=Path(workspace),
        topic=topic,
        state_root=default_state_root(),
        protocol_version=ReviewProtocolVersion(protocol_version),
        issue_ledger_enabled=issue_ledger_enabled,
        counterexample_ballot=counterexample_ballot,
        target_branch=target_branch or None,
        use_worktree=use_worktree,
        integrate_to_target=integrate_to_target,
        allowed_paths=_split_paths(allowed_paths),
        validation_command_lines=_split_lines(validation_commands),
        setup_command_lines=_split_lines(setup_commands),
        participants=tuple(AgentName(agent) for agent in participants),
        first_agent=AgentName(first_agent),
        max_turns=max_turns,
        total_timeout_seconds=total_timeout_minutes * 60,
        max_process_failures=max_process_failures,
        profiles=profiles,
        startup_timeout_seconds=startup_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        hard_timeout_seconds=hard_timeout_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        realtime_output_enabled=realtime_output_enabled,
        raw_output_tail_lines=raw_output_tail_lines,
        mcp_mode=mcp_mode,
        proxy_environment_allowlist=proxy_environment_allowlist,
        facilitator_enabled=facilitator_enabled,
        facilitator_model=facilitator_model,
        allow_network=allow_network,
        unrestricted_commands=unrestricted_commands,
    )


def _render_new_run() -> None:
    """渲染新建运行页；无参数和返回值，点击按钮时执行预检或启动。"""
    st.markdown('<div class="debate-kicker">New debate run</div>', unsafe_allow_html=True)
    st.title("Debate Control Center")
    st.write("选择多个 CLI Agent，串行接力实现、审查和验证同一个目标。")
    default_workspace = os.environ.get("DISCUSS_AGENT_WORKSPACE", str(Path.cwd()))
    workspace = st.text_input(
        "工作空间",
        value=default_workspace,
        help="输入后按 Enter 或移出焦点，页面会读取该目录的当前 Git 分支。",
    )
    detected = None
    with suppress(OSError, ValueError):
        detected = detect_branch(Path(workspace))
    predicted = infer_target_branch(Path(workspace))
    if detected:
        st.caption(f"当前分支：{detected}")
    else:
        st.caption(f"未初始化 Git；将创建分支：{predicted}")
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    definitions = agent_definitions()
    default_commands = {
        definition.name: shutil.which(definition.default_command)
        or definition.default_command
        for definition in definitions
    }
    default_models: dict[AgentName, str] = {}
    for definition in definitions:
        discovered_model = discover_cli_model(
            definition.name,
            home=home,
            environment=os.environ,
        )
        if discovered_model == CLI_DEFAULT_MODEL:
            default_model = ""
        elif discovered_model:
            default_model = discovered_model
        elif definition.name is AgentName.OPENCODE:
            default_model = DEFAULT_OPENCODE_MODEL
        elif definition.name is AgentName.HERMES:
            default_model = "deepseek-v4-flash"
        else:
            default_model = ""
        default_models[definition.name] = default_model
    with st.form("new-run-form"):
        topic = st.text_area(
            "任务目标",
            height=150,
            placeholder="说明要实现什么、完成标准、必须覆盖的测试，以及不应改动的内容。",
        )
        participants = tuple(
            st.multiselect(
                "参与讨论的 CLI Agent",
                tuple(definition.name.value for definition in definitions),
                default=tuple(
                    definition.name.value for definition in definitions[:2]
                ),
                format_func=lambda value: (
                    f"{agent_definition(AgentName(value)).display_name} CLI"
                    + (
                        "（实验性）"
                        if agent_definition(AgentName(value)).experimental
                        else ""
                    )
                ),
                help="至少选择两个；任务启动后参与者列表将被冻结。",
            )
        )
        col_scope, col_validation = st.columns(2)
        with col_scope:
            allowed_paths = st.text_area(
                "允许修改的路径",
                value=".",
                height=100,
                help="每行或逗号分隔。使用 . 表示允许整个仓库。",
            )
        with col_validation:
            validation_commands = st.text_area(
                "验证命令",
                value="",
                height=100,
                placeholder="例如：python -m pytest -q",
                help="可留空；每行一条命令，系统会解析为 argv且不经过 shell。",
            )
        facilitator_enabled = st.toggle("启用 DeepSeek Facilitator", value=True)
        integrate_to_target = st.toggle(
            "完成后合并回目标分支",
            value=True,
            help=(
                "开启后把最终聚焦提交快进到目标分支并清理临时分支；"
                "关闭后目标分支保持不变，并保留 debate 结果分支供继续修改。"
            ),
        )
        with st.expander("高级设置"):
            protocol_version = st.selectbox(
                "审查协议版本",
                tuple(version.value for version in ReviewProtocolVersion),
                index=1,
                help="新 Run 默认使用 review-v1；legacy-v0 仅用于兼容既有协议。",
            )
            issue_ledger_enabled = st.toggle(
                "启用 Issue Ledger",
                value=False,
                disabled=protocol_version != ReviewProtocolVersion.REVIEW_V1.value,
                help="默认关闭；启用后以单一账本记录 review-v1 的阻断 Issue 生命周期。",
            )
            counterexample_ballot = st.toggle(
                "启用反例式 Blind Ballot",
                value=False,
                disabled=protocol_version != ReviewProtocolVersion.REVIEW_V1.value,
                help=(
                    "默认关闭；启用后每位终局投票 Agent 必须给出最强反例及其处理说明。"
                ),
            )
            use_worktree = st.toggle(
                "使用独立 Git worktree",
                value=False,
                help="默认在原工作空间切换到临时 debate 分支；开启后使用独立 worktree。",
            )
            target_branch = st.text_input(
                "目标分支覆盖",
                value=detected or "",
                placeholder=predicted,
                help="留空时使用工作空间当前分支。",
                key=f"target-branch::{workspace}",
            )
            first_agent = st.selectbox(
                "首位 Writer",
                participants or (definitions[0].name.value,),
            )
            max_turns = st.number_input("最大写入回合", 1, 50, 8)
            total_timeout_minutes = st.number_input("总超时（分钟）", 5, 1440, 120)
            max_process_failures = st.number_input(
                "基础设施失败重试次数", 1, 10, 2
            )
            profile_commands: dict[AgentName, str] = {}
            profile_models: dict[AgentName, str] = {}
            profile_variants: dict[AgentName, str] = {}
            profile_native_profiles: dict[AgentName, str] = {}
            profile_providers: dict[AgentName, str] = {}
            cli_columns = st.columns(2)
            for index, definition in enumerate(definitions):
                with cli_columns[index % len(cli_columns)]:
                    command_help = (
                        f"{definition.display_name} 默认以 "
                        f"{definition.default_command} --yolo 模式启动。"
                        if definition.model_policy is ModelPolicy.CLI_DEFAULT
                        else "建议使用自动发现的绝对路径；启动健康检查会冻结路径和版本。"
                    )
                    if definition.name is AgentName.OPENCODE:
                        model_help = (
                            f"默认使用 {DEFAULT_OPENCODE_MODEL}；可改为其他单个 "
                            "provider/model，启动健康检查会验证并冻结该模型。"
                        )
                    else:
                        model_help = (
                            f"留空时使用 {definition.display_name} CLI 当前账号的默认模型。"
                            if definition.model_policy is ModelPolicy.CLI_DEFAULT
                            else (
                                f"留空时从本机 {definition.display_name} 配置发现；"
                                "无法发现会阻止启动。"
                            )
                        )
                    profile_commands[definition.name] = st.text_input(
                        f"{definition.display_name} CLI 路径",
                        value=default_commands[definition.name],
                        help=command_help,
                    )
                    profile_models[definition.name] = st.text_input(
                        f"{definition.display_name} 模型",
                        value=default_models[definition.name],
                        help=model_help,
                    )
                    if definition.supports_variant:
                        profile_variants[definition.name] = st.text_input(
                            f"{definition.display_name} "
                            f"{definition.variant_label or 'Variant'}",
                            value="",
                            help=(
                                "可选；会在预检后冻结，并传给对应 CLI。"
                            ),
                        )
                    if definition.supports_native_profile:
                        profile_native_profiles[definition.name] = st.text_input(
                            f"{definition.display_name} Agent/Profile",
                            value=(
                                "main"
                                if definition.name is AgentName.OPENCLAW
                                else ""
                            ),
                            help=(
                                f"{definition.display_name} 原生 Agent/Profile；"
                                "可留空，配置后会在预检后冻结到本次运行。"
                            ),
                        )
                    if definition.supports_provider:
                        profile_providers[definition.name] = st.text_input(
                            f"{definition.display_name} Provider",
                            value=(
                                "deepseek"
                                if definition.name is AgentName.HERMES
                                else ""
                            ),
                            help=(
                                "Hermes 首版必须显式选择 Provider；Prompt 会出现在"
                                "本机进程 argv，但不会写入审计 argv。"
                            ),
                        )
            timeout_start, timeout_idle, timeout_hard, timeout_heartbeat = st.columns(4)
            startup_timeout_seconds = timeout_start.number_input(
                "启动超时（秒）", 1, 7200, 300
            )
            idle_timeout_seconds = timeout_idle.number_input(
                "空闲超时（秒）", 1, 7200, 300
            )
            hard_timeout_seconds = timeout_hard.number_input(
                "单次硬上限（秒）", 1, 21600, 3600
            )
            heartbeat_interval_seconds = timeout_heartbeat.number_input(
                "心跳间隔（秒）", 1, 300, 5
            )
            realtime_output_enabled = st.toggle("显示 CLI 实时输出", value=True)
            raw_output_tail_lines = st.number_input(
                "原始输出尾部行数", 1, 5000, 500
            )
            mcp_mode = st.selectbox(
                "MCP 模式",
                ("minimal", "full"),
                help="默认 minimal；仅在确实需要本机 MCP 时选择 full。",
            )
            proxy_environment_allowlist = tuple(
                st.multiselect(
                    "允许传入 CLI 的代理/证书变量名",
                    (
                        "HTTPS_PROXY",
                        "HTTP_PROXY",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "SSL_CERT_FILE",
                        "SSL_CERT_DIR",
                    ),
                    help="只保存变量名，不在页面或运行配置中显示变量值。",
                )
            )
            setup_commands = st.text_area("环境准备命令", value="", height=80)
            facilitator_model = st.text_input(
                "Facilitator 模型", value="deepseek-v4-flash"
            )
            unrestricted_commands = st.toggle(
                "完全授权执行命令",
                value=True,
                help=(
                    "可信工作区模式：不限制 sh、rg、python、git 或联网命令；"
                    "仍保留单 Writer、工作空间范围检查和 Git 检查点。"
                ),
            )
            allow_network = st.toggle("限制模式下允许已批准命令联网", value=False)
        left, right, _ = st.columns([1, 1, 3])
        check_clicked = left.form_submit_button("检查配置", use_container_width=True)
        start_clicked = right.form_submit_button(
            "开始讨论", type="primary", use_container_width=True
        )
    if not (check_clicked or start_clicked):
        st.info("页面不会保存密钥，也不会把 DeepSeek Key 传给参与的 CLI Agent。")
        return
    try:
        draft = _draft_from_form(
            workspace=workspace,
            topic=topic,
            protocol_version=protocol_version,
            issue_ledger_enabled=issue_ledger_enabled,
            counterexample_ballot=counterexample_ballot,
            target_branch=target_branch,
            use_worktree=use_worktree,
            integrate_to_target=integrate_to_target,
            allowed_paths=allowed_paths,
            validation_commands=validation_commands,
            setup_commands=setup_commands,
            participants=participants,
            first_agent=first_agent,
            max_turns=int(max_turns),
            total_timeout_minutes=int(total_timeout_minutes),
            max_process_failures=int(max_process_failures),
            profiles=build_profiles_from_form(
                commands=profile_commands,
                models=profile_models,
                variants=profile_variants,
                native_profiles=profile_native_profiles,
                providers=profile_providers,
            ),
            startup_timeout_seconds=int(startup_timeout_seconds),
            idle_timeout_seconds=int(idle_timeout_seconds),
            hard_timeout_seconds=int(hard_timeout_seconds),
            heartbeat_interval_seconds=int(heartbeat_interval_seconds),
            realtime_output_enabled=realtime_output_enabled,
            raw_output_tail_lines=int(raw_output_tail_lines),
            mcp_mode=cast(Literal["minimal", "full"], mcp_mode),
            proxy_environment_allowlist=proxy_environment_allowlist,
            facilitator_enabled=facilitator_enabled,
            facilitator_model=facilitator_model,
            allow_network=allow_network,
            unrestricted_commands=unrestricted_commands,
        )
        config = draft.to_config()
        inspection = inspect_workspace(
            config.workspace,
            expected_branch=config.target_branch,
            profiles=config.profiles,
            participants=config.participants,
        )
    except (OSError, ValueError) as exc:
        st.error(str(exc))
        return
    _render_inspection(inspection)
    st.code(
        json.dumps(config.validation_commands, ensure_ascii=False, indent=2),
        language="json",
    )
    if start_clicked and inspection.can_start:
        try:
            launched = RunLauncher().start(config)
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return
        st.session_state["selected_run_id"] = launched.run_id
        st.session_state["page"] = "运行监控"
        st.rerun()
