"""集中管理 Debate Agent 当前路径与旧版兼容路径。"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

AUDIT_ROOT = PurePosixPath(".agent") / "debate"
LEGACY_AUDIT_ROOT = PurePosixPath(".agent") / "discuss"


def audit_relative_paths(run_id: str) -> tuple[str, str]:
    """返回审计候选路径。

    参数：run_id 为运行标识。
    返回值：当前路径和旧版兼容路径。
    异常：不主动校验 run_id；调用方负责运行标识合法性。
    """
    return (
        (AUDIT_ROOT / run_id).as_posix(),
        (LEGACY_AUDIT_ROOT / run_id).as_posix(),
    )


def select_audit_relative_path(worktree: Path, run_id: str) -> str:
    """选择当前运行的审计路径。

    参数：worktree 为工作树，run_id 为运行标识。
    返回值：旧审计目录已存在时返回旧路径，否则返回当前路径。
    异常：路径解析失败时传播 OSError。
    """
    current, legacy = audit_relative_paths(run_id)
    root = worktree.expanduser().resolve()
    if not (root / current).exists() and (root / legacy).exists():
        return legacy
    return current
