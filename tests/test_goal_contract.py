from __future__ import annotations

from pathlib import Path

import pytest

from debate_agent.goal_contract import (
    GoalContractError,
    GoalContractStore,
)
from debate_agent.models import AgentName
from debate_agent.policy import CommandPolicy, PolicyViolationError


def policy() -> CommandPolicy:
    return CommandPolicy(
        approved_commands=[
            ("python", "-m", "pytest"),
            ("python", "-m", "compileall"),
        ]
    )


def test_goal_contract_is_immutable_additive_and_requires_both_confirmations(
    tmp_path: Path,
) -> None:
    store = GoalContractStore(state_root=tmp_path, run_id="RUN-1")
    contract = store.create(
        objective="实现辩论 Agent",
        deliverables=["agent/debate"],
        acceptance_criteria=["双方确认完成"],
        baseline_validation_commands=[["python", "-m", "pytest", "-q"]],
        allowed_scope=["agent/debate"],
        protected_scope=[".git", ".env"],
        immutable_constraints=["不得 push"],
        command_policy=policy(),
    )

    store.confirm(AgentName.CODEX, version=1)
    with pytest.raises(GoalContractError, match="双方"):
        store.assert_both_confirmed(version=1)
    store.confirm(AgentName.CLAUDE, version=1)
    store.assert_both_confirmed(version=1)

    assert contract.version == 1
    assert contract.objective == "实现辩论 Agent"
    assert contract.effective_scope == ("agent/debate",)


def test_goal_contract_evolution_only_adds_agreed_scope_validation_and_criteria(
    tmp_path: Path,
) -> None:
    store = GoalContractStore(state_root=tmp_path, run_id="RUN-1")
    original = store.create(
        objective="goal",
        deliverables=["src"],
        acceptance_criteria=["base"],
        baseline_validation_commands=[["python", "-m", "pytest"]],
        allowed_scope=["src"],
        protected_scope=[".git"],
        immutable_constraints=["no push"],
        command_policy=policy(),
    )

    with pytest.raises(GoalContractError, match="双方一致"):
        store.evolve(
            original,
            acceptance_additions=["new criterion"],
            validation_additions=[["python", "-m", "compileall", "src"]],
            derived_scope_additions=["tests"],
            scope_approvals={AgentName.CODEX},
            command_policy=policy(),
        )

    evolved = store.evolve(
        original,
        acceptance_additions=["new criterion"],
        validation_additions=[["python", "-m", "compileall", "src"]],
        derived_scope_additions=["tests"],
        scope_approvals={AgentName.CODEX, AgentName.CLAUDE},
        command_policy=policy(),
    )

    assert evolved.version == 2
    assert evolved.objective == original.objective
    assert evolved.deliverables == original.deliverables
    assert evolved.baseline_validation_commands == original.baseline_validation_commands
    assert evolved.acceptance_criteria == ("base", "new criterion")
    assert evolved.added_validation_commands == (("python", "-m", "compileall", "src"),)
    assert evolved.effective_scope == ("src", "tests")


def test_goal_contract_rejects_unapproved_commands_and_scope_expansion(
    tmp_path: Path,
) -> None:
    store = GoalContractStore(state_root=tmp_path, run_id="RUN-1")
    with pytest.raises(PolicyViolationError):
        store.create(
            objective="goal",
            deliverables=["src"],
            acceptance_criteria=["base"],
            baseline_validation_commands=[["curl", "https://example.com"]],
            allowed_scope=["src"],
            protected_scope=[".git"],
            immutable_constraints=["no network"],
            command_policy=policy(),
        )

    with pytest.raises(GoalContractError, match="目标扩展"):
        store.assert_requested_paths_allowed(
            allowed_scope=["src"], derived_scope=[], requested_paths=["unrelated"]
        )


@pytest.mark.parametrize(
    "invalid_command",
    [
        ["git diff --check HEAD^ HEAD"],
        ["校验工作树干净、提交仅修改目标文档"],
    ],
)
def test_goal_contract_rejects_non_argv_validation_additions(
    tmp_path: Path, invalid_command: list[str]
) -> None:
    """验证契约拒绝 shell 字符串和自然语言；参数为目录/命令，无返回值。"""
    store = GoalContractStore(state_root=tmp_path, run_id="RUN-1")
    unrestricted = CommandPolicy(unrestricted_commands=True)

    with pytest.raises(GoalContractError, match="拆分 argv"):
        store.create(
            objective="goal",
            deliverables=["src"],
            acceptance_criteria=["base"],
            baseline_validation_commands=[invalid_command],
            allowed_scope=["src"],
            protected_scope=[".git"],
            immutable_constraints=["no push"],
            command_policy=unrestricted,
        )
