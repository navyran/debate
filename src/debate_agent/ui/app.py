"""Debate Control Center 的 Streamlit 单页入口。"""

# ruff: noqa: E501

from __future__ import annotations

import os

import streamlit as st

from debate_agent.ui.constants import default_state_root
from debate_agent.ui.live_run_page import _render_live_run
from debate_agent.ui.new_run_page import _render_new_run
from debate_agent.ui.styles import _inject_styles
from debate_agent.ui.view_models import list_run_summaries


def _render_monitor() -> None:
    """渲染运行监控入口；无参数和返回值，选择 run 后启动局部轮询。"""
    st.markdown('<div class="debate-kicker">Live execution</div>', unsafe_allow_html=True)
    st.title("运行监控")
    summaries = list_run_summaries(default_state_root())
    if not summaries:
        st.info("还没有运行。先从“新建讨论”启动一个任务。")
        return
    ids = [summary.run_id for summary in summaries]
    preferred = st.session_state.get("selected_run_id")
    index = ids.index(preferred) if preferred in ids else 0
    run_id = st.selectbox("选择运行", ids, index=index)
    st.session_state["selected_run_id"] = run_id
    _render_live_run(run_id)


def _render_history() -> None:
    """渲染历史运行页；无参数和返回值，只读取脱敏摘要。"""
    st.markdown('<div class="debate-kicker">Run archive</div>', unsafe_allow_html=True)
    st.title("历史运行")
    summaries = list_run_summaries(default_state_root())
    if not summaries:
        st.info("运行记录会在这里按新到旧排列。")
        return
    st.dataframe(
        [
            {
                "Run ID": item.run_id,
                "状态": item.status,
                "阶段": item.phase or "--",
                "回合": f"{item.current_turn or 0}/{item.max_turns}",
                "分支": item.target_branch,
                "完成后处理": (
                    "合并目标分支" if item.integrate_to_target else "保留结果分支"
                ),
                "任务": item.topic[:100],
                "工作空间": item.workspace,
            }
            for item in summaries
        ],
        use_container_width=True,
        hide_index=True,
    )
    selected = st.selectbox("打开运行", [item.run_id for item in summaries])
    if st.button("查看运行"):
        st.session_state["selected_run_id"] = selected
        st.session_state["page"] = "运行监控"
        st.rerun()


def main() -> None:
    """渲染控制中心；无参数和返回值，页面动作通过受控服务执行。"""
    st.set_page_config(
        page_title="Debate Control Center",
        page_icon="⇄",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_styles()
    st.sidebar.markdown("## ⇄ Debate")
    st.sidebar.caption("双 Agent 共享工作树控制台")
    pages = ("新建讨论", "运行监控", "历史运行")
    requested = st.session_state.get("page", "新建讨论")
    page = st.sidebar.radio(
        "导航",
        pages,
        index=pages.index(requested) if requested in pages else 0,
        label_visibility="collapsed",
    )
    st.session_state["page"] = page
    st.sidebar.divider()
    st.sidebar.caption(f"状态目录\n\n`{default_state_root()}`")
    st.sidebar.caption(
        "DeepSeek：" + ("已配置" if os.environ.get("DEEPSEEK_API_KEY") else "降级模式")
    )
    if page == "新建讨论":
        _render_new_run()
    elif page == "运行监控":
        _render_monitor()
    else:
        _render_history()


main()
