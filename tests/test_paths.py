from __future__ import annotations

from pathlib import Path

from debate_agent.paths import audit_relative_paths, select_audit_relative_path


def test_audit_paths_default_to_debate_and_resume_existing_legacy_tree(
    tmp_path: Path,
) -> None:
    """验证新运行使用 debate 路径且旧运行继续使用已有 discuss 审计目录。"""
    current, legacy = audit_relative_paths("RUN-1")

    assert current == ".agent/debate/RUN-1"
    assert legacy == ".agent/discuss/RUN-1"
    assert select_audit_relative_path(tmp_path, "RUN-1") == current

    (tmp_path / legacy).mkdir(parents=True)

    assert select_audit_relative_path(tmp_path, "RUN-1") == legacy


def test_current_audit_tree_wins_if_both_namespaces_exist(tmp_path: Path) -> None:
    """验证迁移中两目录并存时优先当前路径；参数为临时目录，无返回值。"""
    current, legacy = audit_relative_paths("RUN-2")
    (tmp_path / current).mkdir(parents=True)
    (tmp_path / legacy).mkdir(parents=True)

    assert select_audit_relative_path(tmp_path, "RUN-2") == current
