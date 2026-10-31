"""Debate Control Center 的运行监控页面。"""

# ruff: noqa: E501

from __future__ import annotations

import html
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import streamlit as st

from debate_agent.agent_catalog import (
    agent_definition,
    agent_definitions,
)
from debate_agent.artifacts import ArtifactError, ArtifactStore
from debate_agent.events import RunEvent, RunEventStore
from debate_agent.invocation_events import InvocationEvent, Redactor
from debate_agent.issue_ledger import IssueLedgerError, IssueLedgerStore
from debate_agent.launcher import RunLauncher
from debate_agent.models import AgentName, ReviewProtocolVersion, RunStatus
from debate_agent.runtime import DebateLifecycleService
from debate_agent.ui.constants import (
    PHASE_ORDER,
    PHASES,
    TERMINAL_STATUSES,
    default_state_root,
)
from debate_agent.ui.view_models import (
    AgentLiveTrack,
    read_invocation_live_view,
    read_raw_output_tails,
)


def _phase_step_classes(phase: str, *, status: str) -> tuple[str, ...]:
    """计算阶段节点样式。

    参数为最近事件阶段和运行状态；返回每个阶段的 CSS 类；未知阶段按首节点处理，
    不抛异常。运行已完成时以权威终态覆盖可能滞后的最后事件阶段。
    """
    active = PHASE_ORDER.get(phase, 0)
    completed = status == RunStatus.COMPLETED.value
    return tuple(
        "done" if completed or index < active else "active" if index == active else ""
        for index, _phase in enumerate(PHASES)
    )


def _render_phase_rail(phase: str, *, status: str) -> None:
    """展示真实阶段轨道；参数为当前阶段/运行状态，无返回值，不计算虚假百分比。"""
    blocks = []
    classes = _phase_step_classes(phase, status=status)
    for css, (_key, label) in zip(classes, PHASES, strict=True):
        blocks.append(
            f'<div class="phase-step {css}">{html.escape(label)}</div>'
        )
    st.markdown(
        f'<div class="phase-rail">{"".join(blocks)}</div>',
        unsafe_allow_html=True,
    )


def _latest_actor_event(events: list[RunEvent], actor: str) -> RunEvent | None:
    """读取某角色最新事件；参数为事件和角色，返回事件或 None。"""
    return next((event for event in reversed(events) if event.actor == actor), None)


def _agent_lane_content(
    actor: str,
    events: list[RunEvent],
    track: AgentLiveTrack,
) -> tuple[str, str]:
    """生成 Agent 摘要文案。

    参数为角色、节点事件和调用轨道；返回动作/元信息；没有 Writer 事件但已有审核、
    投票或确认调用时，不误报为尚未参与。
    """
    latest = _latest_actor_event(events, actor)
    if track.event_count > 0:
        action = track.last_title
        if latest:
            meta = (
                f"最近 Writer TURN {latest.current_turn or 0} · "
                f"{latest.head[:10] if latest.head else 'HEAD 待定'}"
            )
        else:
            call_state = "最近调用已完成" if track.terminal else "正在执行"
            meta = f"已参与非 Writer 调用 · {call_state}"
        return action, meta
    if latest:
        return (
            latest.title,
            f"Writer TURN {latest.current_turn or 0} · "
            f"{latest.head[:10] if latest.head else 'HEAD 待定'}",
        )
    return "等待首次调用", "尚未参与当前运行"


def _render_agent_lane(
    agent: AgentName,
    events: list[RunEvent],
    track: AgentLiveTrack,
) -> None:
    """展示 Agent 接力状态；参数为 Agent、节点事件和调用轨道，无返回值。"""
    actor = agent.value
    action, meta = _agent_lane_content(actor, events, track)
    st.markdown(
        f'<div class="agent-lane {actor}">'
        f'<div class="agent-name">{html.escape(agent_definition(agent).display_name)}</div>'
        f'<div class="agent-action">{html.escape(action)}</div>'
        f'<div class="agent-meta mono">{html.escape(meta)}</div></div>',
        unsafe_allow_html=True,
    )


def _render_invocation_track(
    track: AgentLiveTrack,
    events: tuple[InvocationEvent, ...],
) -> None:
    """展示单个 CLI 的实时轨道。

    参数为 Agent 快照和受限事件；无返回值；事件正文使用安全代码块，不解释 HTML。
    """
    elapsed, idle, event_count, files = st.columns(4)
    elapsed.metric("运行秒数", f"{track.elapsed_seconds:.1f}")
    idle.metric("空闲秒数", f"{track.idle_seconds:.1f}")
    event_count.metric("事件数", track.event_count)
    files.metric("变更文件", track.changed_file_count)
    st.caption(f"最近活动：{track.last_title}")
    if track.termination_category:
        st.warning(f"终止类别：{track.termination_category}")
    if not events:
        display_name = agent_definition(track.agent).display_name
        st.info(f"{display_name} 尚无新的 CLI 调用事件。")
        return
    for event in events[-100:]:
        timestamp = event.timestamp.astimezone().strftime("%H:%M:%S")
        st.caption(
            f"#{event.seq} · {timestamp} · {event.event_type.value} · "
            f"运行 {event.elapsed_seconds:.1f}s / 空闲 {event.idle_seconds:.1f}s"
        )
        st.write(event.title)
        if event.summary:
            st.code(event.summary, language=None)


def _run_redactor(store: ArtifactStore) -> Redactor:
    """构造 raw 页面脱敏器。

    参数为运行存储；返回包含已知 Prompt、凭据和 Session 的脱敏器；损坏的 Session
    文件会被忽略，不向页面暴露环境变量值。
    """
    config = store.load_config()
    secret_values = [config.topic]
    sensitive_names = {
        config.facilitator_api_key_env,
        *config.proxy_environment_allowlist,
    }
    secret_values.extend(
        value
        for name, value in os.environ.items()
        if name in sensitive_names and value
    )
    session_ids: list[str] = []
    session_path = store.run_dir / "agent-sessions.json"
    with suppress(OSError, json.JSONDecodeError, TypeError, AttributeError):
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        sessions = payload.get("sessions", {})
        for agent in config.participants:
            session_id = sessions.get(agent.value, {}).get("session_id")
            if session_id:
                session_ids.append(str(session_id))
    return Redactor(
        workspace=config.workspace,
        secret_values=tuple(secret_values),
        session_ids=tuple(session_ids),
    )


def _merge_invocation_events(
    previous: tuple[InvocationEvent, ...],
    current: tuple[InvocationEvent, ...],
    *,
    limit: int,
) -> tuple[InvocationEvent, ...]:
    """合并去重实时事件；参数为前后批次和上限，返回有序尾窗，不抛异常。"""
    merged = {event.seq: event for event in (*previous, *current)}
    ordered = tuple(merged[seq] for seq in sorted(merged))
    return ordered[-limit:]


def _render_timeline(events: list[RunEvent]) -> None:
    """展示脱敏接力时间线；参数为事件列表，无返回值，不展示原始模型内容。"""
    if not events:
        st.info("运行事件尚未产生。")
        return
    blocks = []
    agent_actors = {
        definition.name.value for definition in agent_definitions()
    }
    for event in events[-30:]:
        actor = (
            event.actor
            if event.actor in agent_actors | {"facilitator"}
            else "supervisor"
        )
        timestamp = event.timestamp.astimezone().strftime("%H:%M:%S")
        summary = (
            f'<div>{html.escape(event.summary)}</div>' if event.summary else ""
        )
        blocks.append(
            f'<div class="relay-event {actor}"><div class="event-head">'
            f"<span>{html.escape(event.title)}</span><small>{timestamp}</small>"
            f"</div>{summary}<small>{html.escape(event.phase)} · "
            f"{html.escape(event.head[:10] if event.head else 'no-head')}</small></div>"
        )
    st.markdown(
        f'<div class="relay">{"".join(blocks)}</div>', unsafe_allow_html=True
    )


def _read_json_files(directory: Path) -> list[dict[str, Any]]:
    """读取目录中的结构化 JSON；参数为目录，返回有效对象列表，损坏文件会跳过。"""
    rows: list[dict[str, Any]] = []
    if not directory.exists():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["_record"] = path.name
            rows.append(payload)
    return rows


def _render_structured_tabs(store: ArtifactStore) -> None:
    """展示变化、验证、结论和 Facilitator；参数为运行存储，无返回值。"""
    changes, validation, completion, facilitator, ledger_tab, config_tab = st.tabs(
        ("代码变化", "验证结果", "参与者结论", "Facilitator", "Issue Ledger", "运行配置")
    )
    with changes:
        rows = _read_json_files(store.run_dir / "updates")
        st.json(rows if rows else {"状态": "尚无交接摘要"}, expanded=False)
    with validation:
        rows = _read_json_files(store.run_dir / "validation")
        st.json(rows if rows else {"状态": "尚无验证报告"}, expanded=False)
    with completion:
        try:
            state = store.read_state()
        except (OSError, json.JSONDecodeError):
            state = {}
        st.json(
            {
                "final_validation_passed": state.get("final_validation_passed"),
                "assessments": state.get("assessments", {}),
                "ballots": state.get("ballots", {}),
                "tree_acks": state.get("tree_acks", {}),
                "final_tree_sha": state.get("final_tree_sha"),
                "integration_succeeded": state.get("integration_succeeded"),
                "integration_skipped": state.get("integration_skipped"),
                "result_branch": state.get("result_branch"),
                "result_worktree": state.get("result_worktree"),
            },
            expanded=False,
        )
    with facilitator:
        rows = _read_json_files(store.run_dir / "facilitator")
        st.json(rows if rows else {"状态": "尚无 Facilitator 决定"}, expanded=False)
    with ledger_tab:
        config = store.load_config()
        if (
            config.protocol_version is ReviewProtocolVersion.REVIEW_V1
            and config.issue_ledger_enabled
        ):
            try:
                ledger = IssueLedgerStore(artifacts=store).load()
            except (ArtifactError, IssueLedgerError, OSError, json.JSONDecodeError) as exc:
                st.error(f"Issue Ledger 损坏：{exc}")
            else:
                st.json(ledger.model_dump(mode="json"), expanded=False)
        else:
            st.info("当前 Run 未启用 review-v1 Issue Ledger。")
    with config_tab:
        config = store.load_config()
        st.json(
            {
                "workspace": str(config.workspace),
                "target_branch": config.target_branch,
                "participants": [agent.value for agent in config.participants],
                "protocol_version": config.protocol_version.value,
                "issue_ledger_enabled": config.issue_ledger_enabled,
                "counterexample_ballot": config.counterexample_ballot,
                "integrate_to_target": config.integrate_to_target,
                "topic": config.topic,
                "allowed_scope": config.allowed_scope,
                "validation_commands": config.validation_commands,
                "max_turns": config.max_turns,
                "facilitator_enabled": config.facilitator_enabled,
                "facilitator_model": config.facilitator_model,
            },
            expanded=False,
        )


def _render_run_controls(store: ArtifactStore, status: str) -> None:
    """展示生命周期按钮；参数为存储/状态，无返回值，点击时执行对应动作。"""
    left, middle, right, _ = st.columns([1, 1, 1, 3])
    if left.button(
        "中止运行",
        disabled=status != RunStatus.RUNNING.value,
        use_container_width=True,
    ):
        DebateLifecycleService().abort(
            state_root=store.state_root, run_id=store.run_id
        )
        st.rerun()
    recoverable = status in TERMINAL_STATUSES - {
        RunStatus.COMPLETED.value,
        RunStatus.ABORTED.value,
    }
    if middle.button("恢复运行", disabled=not recoverable, use_container_width=True):
        RunLauncher().resume(state_root=store.state_root, run_id=store.run_id)
        st.rerun()
    if right.button(
        "清理现场",
        disabled=status == RunStatus.RUNNING.value,
        use_container_width=True,
    ):
        DebateLifecycleService().cleanup(
            state_root=store.state_root, run_id=store.run_id
        )
        st.success("运行现场已清理。")


@st.fragment(run_every=2)
def _render_live_run(run_id: str) -> None:
    """轮询并展示单个运行；参数为 run ID，无返回值，文件损坏时显示错误。"""
    try:
        store = ArtifactStore.open(state_root=default_state_root(), run_id=run_id)
        status_payload = store.read_status()
        event_store = RunEventStore(state_root=default_state_root(), run_id=run_id)
        progress = event_store.read_progress()
        events = event_store.read_events(limit=100)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        st.error(str(exc))
        return
    status = str(status_payload.get("status", "unknown"))
    title = progress.title if progress else "等待 Worker 更新"
    updated = progress.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if progress else "--"
    st.markdown(
        '<div class="status-shell"><div>'
        f"<strong>{html.escape(title)}</strong>"
        f"<small>{html.escape(run_id)} · 最近更新 {updated}</small></div>"
        f'<div class="status-pill">{html.escape(status.upper())}</div></div>',
        unsafe_allow_html=True,
    )
    phase = progress.phase if progress else "queued"
    _render_phase_rail(phase, status=status)
    config = store.load_config()
    cursor_key = f"invocation-cursor::{run_id}"
    cache_key = f"invocation-events::{run_id}"
    after_seq = int(st.session_state.get(cursor_key, 0))
    cached_events = tuple(st.session_state.get(cache_key, ()))
    live = read_invocation_live_view(
        state_root=default_state_root(),
        run_id=run_id,
        after_seq=after_seq,
        limit=config.invocation_event_limit,
    )
    merged_events = _merge_invocation_events(
        cached_events,
        live.events,
        limit=config.invocation_event_limit,
    )
    st.session_state[cache_key] = merged_events
    st.session_state[cursor_key] = live.last_seq
    if live.warning:
        st.warning(live.warning)

    tabs = st.tabs(
        (
            "进度摘要",
            *(
                f"{agent_definition(agent).display_name} 实时"
                for agent in config.participants
            ),
            "原始输出",
            "验证与结论",
        )
    )
    progress_tab = tabs[0]
    agent_tabs = tabs[1 : 1 + len(config.participants)]
    raw_tab = tabs[-2]
    result_tab = tabs[-1]
    with progress_tab:
        metric_status, metric_turn, metric_actor, metric_head = st.columns(4)
        metric_status.metric("当前阶段", phase)
        metric_turn.metric(
            "写入回合",
            f"{progress.current_turn if progress and progress.current_turn is not None else 0} / "
            f"{progress.max_turns if progress and progress.max_turns else config.max_turns}",
        )
        metric_actor.metric(
            "当前角色",
            progress.actor if progress and progress.actor else "supervisor",
        )
        metric_head.metric(
            "当前 HEAD",
            progress.head[:10] if progress and progress.head else "待创建",
        )
        lanes = st.columns(len(config.participants))
        for lane, agent in zip(lanes, config.participants, strict=True):
            with lane:
                _render_agent_lane(agent, events, live.tracks[agent])
        st.subheader("提交接力轨")
        _render_timeline(events)
    for tab, agent in zip(agent_tabs, config.participants, strict=True):
        with tab:
            _render_invocation_track(
                live.tracks[agent],
                tuple(event for event in merged_events if event.agent is agent),
            )
    with raw_tab:
        st.caption("仅显示最后若干行；Prompt、凭据、Session 与外部绝对路径会脱敏。")
        with st.expander("显示脱敏 raw 尾部", expanded=False):
            tails = read_raw_output_tails(
                state_root=default_state_root(),
                run_id=run_id,
                redactor=_run_redactor(store),
                lines=config.raw_output_tail_lines,
            )
            if not tails:
                st.info("尚无 raw 输出文件。")
            for tail in tails:
                st.caption(f"{tail.file_name} · {tail.stream}")
                st.code(tail.text or "<empty>", language=None)
    with result_tab:
        _render_structured_tabs(store)
    if status_payload.get("stop_reason"):
        st.warning(str(status_payload["stop_reason"]))
        categories = {
            track.termination_category
            for track in (
                live.tracks[agent] for agent in config.participants
            )
            if track.termination_category
        }
        if categories & {"startup_timeout", "idle_timeout"}:
            st.info("可先查看 raw 尾部确认 CLI 是否等待输入，再按需调高对应超时后新建运行。")
        elif "network_error" in categories:
            st.info("请检查 CLI 登录状态、模型连通性及显式选择的代理/证书变量名。")
    if status == RunStatus.COMPLETED.value:
        with suppress(OSError, json.JSONDecodeError):
            state = store.read_state()
            if state.get("integration_skipped"):
                st.success("讨论已完成，按启动配置保留结果，未合并目标分支。")
                st.code(
                    "\n".join(
                        (
                            f"结果分支: {state.get('result_branch', '--')}",
                            f"结果工作区: {state.get('result_worktree', '--')}",
                            f"结果提交: {state.get('integration_commit', '--')}",
                        )
                    )
                )
    _render_run_controls(store, status)
