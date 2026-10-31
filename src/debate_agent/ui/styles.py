"""Debate Control Center 的页面样式。"""

# ruff: noqa: E501

from __future__ import annotations

import streamlit as st

from debate_agent.agent_catalog import (
    agent_definition,
    agent_definitions,
)


def _inject_styles() -> None:
    """注入控制台视觉样式；无参数和返回值，不改变业务状态。"""
    definitions = agent_definitions()
    agent_styles = "\n".join(
        (
            f".agent-lane.{agent.value} "
            f"{{ border-color: {agent_definition(agent).ui_color}; }}"
            f" .agent-lane.{agent.value} .agent-name "
            f"{{ color: {agent_definition(agent).ui_color}; }}"
            f" .relay-event.{agent.value}:before "
            f"{{ background: {agent_definition(agent).ui_color}; }}"
        )
        for agent in (definition.name for definition in definitions)
    )
    primary_color = definitions[0].ui_color
    phase_color = definitions[1].ui_color
    styles = """
        <style>
        :root {
          --ink: #17242e;
          --muted: #64717b;
          --paper: #f3f6f8;
          --panel: #ffffff;
          --line: #d8e0e5;
          --primary: __PRIMARY_COLOR__;
          --phase: __PHASE_COLOR__;
          --ok: #26836f;
          --warn: #b17822;
          --danger: #b34343;
        }
        .stApp { background: var(--paper); color: var(--ink); }
        h1, h2, h3 { font-family: "Avenir Next", "Source Sans 3", sans-serif; }
        h1 { letter-spacing: -0.035em; }
        code, .mono { font-family: "SFMono-Regular", "IBM Plex Mono", monospace; }
        .debate-kicker {
          color: var(--primary); font: 700 .74rem/1.2 "SFMono-Regular", monospace;
          letter-spacing: .12em; text-transform: uppercase; margin-bottom: -.45rem;
        }
        .status-shell {
          background: var(--ink); color: #f8fbfc; border-radius: 18px;
          padding: 1.1rem 1.25rem; margin: .35rem 0 1rem;
          display: flex; justify-content: space-between; gap: 1rem; align-items: center;
        }
        .status-shell strong { font-size: 1.12rem; }
        .status-shell small { color: #b9c6ce; display: block; margin-top: .25rem; }
        .status-pill {
          border: 1px solid #71808a; border-radius: 999px; padding: .32rem .72rem;
          font: 700 .72rem/1 "SFMono-Regular", monospace; white-space: nowrap;
        }
        .phase-rail { display: grid; grid-template-columns: repeat(7, 1fr); gap: 0; margin: 1rem 0 1.4rem; }
        .phase-step { position: relative; color: var(--muted); font-size: .72rem; text-align: center; padding-top: 1.25rem; }
        .phase-step:before { content: ""; width: .62rem; height: .62rem; border-radius: 50%; background: #c8d1d7; position: absolute; top: .25rem; left: calc(50% - .31rem); z-index: 2; }
        .phase-step:after { content: ""; height: 2px; background: #c8d1d7; position: absolute; top: .53rem; left: 0; right: 0; }
        .phase-step:first-child:after { left: 50%; }
        .phase-step:last-child:after { right: 50%; }
        .phase-step.done:before, .phase-step.done:after { background: var(--ok); }
        .phase-step.active { color: var(--ink); font-weight: 700; }
        .phase-step.active:not(:first-child):after { background: linear-gradient(90deg, var(--ok) 0 50%, #c8d1d7 50% 100%); }
        .phase-step.active:last-child:after { background: var(--ok); }
        .phase-step.active:before { background: var(--phase); box-shadow: 0 0 0 5px rgba(207,91,78,.14); }
        .agent-lane { border-top: 3px solid var(--line); background: var(--panel); padding: .9rem 1rem; min-height: 7rem; border-radius: 0 0 12px 12px; }
        .agent-name { font: 800 .76rem/1 "SFMono-Regular", monospace; letter-spacing: .1em; text-transform: uppercase; }
        __AGENT_STYLES__
        .agent-action { font-weight: 700; margin: .6rem 0 .3rem; }
        .agent-meta { color: var(--muted); font-size: .82rem; }
        .relay { position: relative; margin: .5rem 0 1.2rem; }
        .relay:before { content: ""; position: absolute; top: .3rem; bottom: .3rem; left: 1rem; width: 2px; background: var(--line); }
        .relay-event { position: relative; margin: 0 0 .8rem 2.5rem; padding: .7rem .85rem; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
        .relay-event:before { content: ""; position: absolute; left: -2rem; top: .9rem; width: .66rem; height: .66rem; border-radius: 50%; background: var(--ink); border: 3px solid var(--paper); }
        .relay-event.facilitator:before { background: var(--warn); }
        .relay-event .event-head { display: flex; justify-content: space-between; gap: 1rem; font-weight: 700; }
        .relay-event small { color: var(--muted); font-family: "SFMono-Regular", monospace; }
        .check-row { display: grid; grid-template-columns: 1.2rem 10rem 1fr; gap: .55rem; padding: .45rem 0; border-bottom: 1px solid var(--line); font-size: .88rem; }
        .check-success { color: var(--ok); } .check-warning { color: var(--warn); } .check-error { color: var(--danger); }
        @media (max-width: 800px) {
          .phase-rail { grid-template-columns: repeat(4, 1fr); row-gap: .7rem; }
          .phase-step:after { display: none; }
          .status-shell { align-items: flex-start; flex-direction: column; }
        }
        @media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
        </style>
        """
    st.markdown(
        styles.replace("__PRIMARY_COLOR__", primary_color)
        .replace("__PHASE_COLOR__", phase_color)
        .replace("__AGENT_STYLES__", agent_styles),
        unsafe_allow_html=True,
    )
