from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from debate_agent.adapters import CodeBuddyAdapter
from debate_agent.config import AgentProfile
from debate_agent.process import ProcessResult

pytestmark = pytest.mark.skipif(
    os.environ.get("DEBATE_AGENT_E2E_CODEBUDDY") != "1",
    reason="仅在显式启用 CodeBuddy 真实 E2E 时运行",
)


def _run(invocation, *, environment: dict[str, str]) -> ProcessResult:
    """执行真实 CodeBuddy 调用。

    参数为 Adapter Invocation 和隔离环境；返回受控进程结果；
    启动、超时或文件错误由 subprocess 传播。
    """
    effective_environment = dict(environment)
    effective_environment.update(dict(invocation.environment_overrides))
    completed = subprocess.run(
        invocation.argv,
        cwd=invocation.cwd,
        input=invocation.stdin,
        capture_output=True,
        text=True,
        timeout=min(invocation.hard_timeout_seconds, 300),
        env=effective_environment,
        check=False,
        shell=False,
    )
    return ProcessResult(
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_seconds=0,
        raw_output_ref="live-e2e",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        cancelled=False,
    )


def _git_status(workspace: Path) -> str:
    """读取临时仓库状态；参数为工作区，返回 porcelain 文本，Git 失败时抛错。"""
    completed = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def test_codebuddy_live_session_resume_writer_and_readonly_boundary(
    tmp_path: Path,
) -> None:
    """验证真实 CodeBuddy Session 复用、精确写入和只读指纹。

    参数为临时目录；无返回值；CLI、模型或认证缺失时显式失败，
    只有未启用环境开关时由模块级条件跳过。
    """
    executable = shutil.which("codebuddy")
    assert executable is not None, "已启用 E2E，但未安装 codebuddy"
    model = os.environ.get("DEBATE_AGENT_E2E_CODEBUDDY_MODEL", "").strip()
    assert model, "真实 E2E 必须设置 DEBATE_AGENT_E2E_CODEBUDDY_MODEL"
    subprocess.run(("git", "init", "--quiet"), cwd=tmp_path, check=True)
    schema = tmp_path / "probe.schema.json"
    schema.write_text(
        '{"type":"object","properties":{"ok":{"type":"boolean"}},'
        '"required":["ok"],"additionalProperties":false}',
        encoding="utf-8",
    )
    adapter = CodeBuddyAdapter(
        AgentProfile(
            command=executable,
            model=model,
            native_profile=(
                os.environ.get("DEBATE_AGENT_E2E_CODEBUDDY_AGENT", "").strip()
                or None
            ),
        ),
        schema_path=schema,
        minimal_mcp=True,
    )
    environment = dict(os.environ)

    first_invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt='不要调用工具，只返回 {"ok": true}。',
        session_id=None,
    )
    first = adapter.extract_terminal(
        _run(first_invocation, environment=environment)
    )
    assert first.session_id
    assert first.content == {"ok": True}
    assert _git_status(tmp_path) == "?? probe.schema.json\n"

    writer_invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt=(
            "继续同一会话。只创建 codebuddy-writer.txt，内容必须严格为 "
            "`CODEBUDDY-WRITER\\n`，不要修改其他文件；完成后返回 "
            '{"ok": true}。'
        ),
        session_id=first.session_id,
    )
    writer = adapter.extract_terminal(
        _run(writer_invocation, environment=environment)
    )
    assert writer.session_id == first.session_id
    assert writer.content == {"ok": True}
    assert (tmp_path / "codebuddy-writer.txt").read_text(encoding="utf-8") == (
        "CODEBUDDY-WRITER\n"
    )

    before_review = _git_status(tmp_path)
    reviewer_invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt=(
            "继续同一会话。只读取并检查 codebuddy-writer.txt，不得修改文件或执行"
            '写命令；返回 {"ok": true}。'
        ),
        session_id=first.session_id,
    )
    reviewer = adapter.extract_terminal(
        _run(reviewer_invocation, environment=environment)
    )
    assert reviewer.session_id == first.session_id
    assert reviewer.content == {"ok": True}
    assert _git_status(tmp_path) == before_review
