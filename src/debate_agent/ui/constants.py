"""Debate Control Center 的共享界面常量。"""

from __future__ import annotations

import os
from pathlib import Path

from debate_agent.models import RunStatus

APP_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OPENCODE_MODEL = "opencode/deepseek-v4-flash-free"


def default_state_root() -> Path:
    """读取默认状态目录；无参数，返回当前环境对应路径，不抛业务异常。"""
    return Path(
        os.environ.get("DISCUSS_AGENT_STATE_ROOT", APP_ROOT / ".runtime")
    ).expanduser().resolve()

PHASES = (
    ("preflight", "预检"),
    ("environment", "环境"),
    ("debate", "方案讨论"),
    ("final_validation", "最终验证"),
    ("completion", "全员确认"),
    ("audit", "审计"),
    ("integration", "集成"),
)

PHASE_ORDER = {
    "queued": 0,
    "preflight": 0,
    "environment": 1,
    "debate": 2,
    "validation": 2,
    "handoff": 2,
    "facilitator": 2,
    "final_validation": 3,
    "completion": 4,
    "audit": 5,
    "integration": 6,
    "completed": 6,
    "stopped": 2,
    "recovery": 2,
}

TERMINAL_STATUSES = {
    status.value for status in RunStatus if status is not RunStatus.RUNNING
}
