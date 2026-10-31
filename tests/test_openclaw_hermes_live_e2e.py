"""OpenClaw 与 Hermes 的显式启用真实 CLI E2E。

默认测试运行不会收集本文件中的模型测试；只有操作者设置对应的
``DEBATE_AGENT_E2E_*`` 环境变量后，pytest 才会看到相关测试函数。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

from debate_agent.secrets import load_default_deepseek_key

_OPENCLAW_E2E = os.environ.get("DEBATE_AGENT_E2E_OPENCLAW") == "1"
_HERMES_E2E = os.environ.get("DEBATE_AGENT_E2E_HERMES") == "1"
_SESSION_LINE = re.compile(
    r"^\s*session_id:\s*(?P<session>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*$"
)


def test_live_e2e_entrypoints_require_exact_opt_in() -> None:
    """验证真实测试只接受精确值 1；无参数，无模型调用，不抛异常。"""
    assert _OPENCLAW_E2E is (
        os.environ.get("DEBATE_AGENT_E2E_OPENCLAW") == "1"
    )
    assert _HERMES_E2E is (
        os.environ.get("DEBATE_AGENT_E2E_HERMES") == "1"
    )


def _run(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    """执行真实 CLI。

    参数：argv 为无 shell 命令，cwd 为临时仓库，environment 为可选隔离环境，
    timeout 为硬时限。
    返回值：成功的文本进程结果。
    异常：命令失败或超时由 subprocess 原样抛出，输出中不得包含测试 Prompt。
    """
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _init_repo(path: Path) -> None:
    """初始化隔离 Git 仓库；参数为临时目录，无返回值；Git 失败时抛异常。"""
    _run(("git", "init", "-q"), cwd=path, timeout=30)


def _openclaw_text(stdout: str) -> tuple[str, str]:
    """解析 OpenClaw 终止输出。

    参数：stdout 为真实 JSON 输出。
    返回值：规范 Session Key 与最终正文。
    异常：JSON 或必需边界字段缺失时抛断言错误。
    """
    payload = json.loads(stdout)
    result = payload["result"]
    report = result["meta"]["systemPromptReport"]
    texts = [
        item["text"]
        for item in result["payloads"]
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    assert payload["status"] == "ok"
    assert texts
    return report["sessionKey"], texts[-1]


def _hermes_result(stdout: str, stderr: str = "") -> tuple[str, str]:
    """解析 Hermes Quiet 输出。

    参数：stdout 为真实正文，stderr 为 Session 状态。
    返回值：Session ID 与去除状态行后的正文。
    异常：Session 或正文缺失时抛断言错误。
    """
    session_ids: list[str] = []
    body: list[str] = []
    for output in (stdout, stderr):
        for line in output.splitlines():
            match = _SESSION_LINE.fullmatch(line)
            if match is not None:
                session_ids.append(match.group("session"))
    for line in stdout.splitlines():
        if _SESSION_LINE.fullmatch(line) is not None:
            continue
        if line.strip() and not line.lstrip().startswith("↻ Resumed session "):
            body.append(line)
    assert len(set(session_ids)) == 1
    assert body
    return session_ids[0], "\n".join(body)


if _OPENCLAW_E2E:

    def test_openclaw_live_session_isolated_and_reused(tmp_path: Path) -> None:
        """验证 OpenClaw Gateway 的隔离 Session 与原生记忆；参数为临时目录。"""
        _init_repo(tmp_path)
        nonce = f"OPENCLAW_E2E_{uuid4().hex}"
        session_key = f"debate:e2e:{uuid4().hex}"
        first_prompt = tmp_path / "openclaw-first.md"
        second_prompt = tmp_path / "openclaw-second.md"
        first_prompt.write_text(
            f"不要调用工具。记住标记 {nonce}，只回复 OK。",
            encoding="utf-8",
        )
        second_prompt.write_text(
            "不要调用工具。只回复上一轮要求你记住的完整标记。",
            encoding="utf-8",
        )
        base = (
            "openclaw",
            "agent",
            "--agent",
            os.environ.get("DEBATE_AGENT_E2E_OPENCLAW_PROFILE", "main"),
            "--session-key",
            session_key,
            "--thinking",
            "off",
            "--timeout",
            "180",
            "--json",
        )
        model = os.environ.get("DEBATE_AGENT_E2E_OPENCLAW_MODEL", "").strip()
        model_args = ("--model", model) if model else ()
        try:
            first = _run(
                (*base, *model_args, "--message-file", str(first_prompt)),
                cwd=tmp_path,
            )
            canonical, _ = _openclaw_text(first.stdout)
            second = _run(
                (*base, *model_args, "--message-file", str(second_prompt)),
                cwd=tmp_path,
            )
            resumed, body = _openclaw_text(second.stdout)
            assert canonical == resumed
            assert canonical.endswith(session_key)
            assert nonce in body
        finally:
            first_prompt.unlink(missing_ok=True)
            second_prompt.unlink(missing_ok=True)
            subprocess.run(
                (
                    "openclaw",
                    "sessions",
                    "cleanup",
                    "--agent",
                    os.environ.get(
                        "DEBATE_AGENT_E2E_OPENCLAW_PROFILE", "main"
                    ),
                    "--enforce",
                    "--json",
                ),
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )


if _HERMES_E2E:

    def test_hermes_live_session_isolated_reused_and_deleted(
        tmp_path: Path,
    ) -> None:
        """验证 Hermes 真实 Query、Session 记忆及精确清理；参数为临时目录。"""
        _init_repo(tmp_path)
        environment = dict(os.environ)
        load_default_deepseek_key(environment=environment)
        assert environment.get("DEEPSEEK_API_KEY")
        nonce = f"HERMES_E2E_{uuid4().hex}"
        provider = os.environ.get(
            "DEBATE_AGENT_E2E_HERMES_PROVIDER", "deepseek"
        )
        model = os.environ.get(
            "DEBATE_AGENT_E2E_HERMES_MODEL", "deepseek-v4-flash"
        )
        base = (
            "hermes",
            "chat",
            "--quiet",
            "--yolo",
            "--no-restore-cwd",
            "--provider",
            provider,
            "--model",
            model,
        )
        session_id: str | None = None
        try:
            first = _run(
                (
                    *base,
                    "--query",
                    f"不要调用工具。记住标记 {nonce}，只回复 OK。",
                ),
                cwd=tmp_path,
                environment=environment,
            )
            session_id, _ = _hermes_result(first.stdout, first.stderr)
            second = _run(
                (
                    *base,
                    "--resume",
                    session_id,
                    "--query",
                    "不要调用工具。只回复上一轮要求你记住的完整标记。",
                ),
                cwd=tmp_path,
                environment=environment,
            )
            resumed, body = _hermes_result(second.stdout, second.stderr)
            assert resumed == session_id
            assert nonce in body
        finally:
            if session_id is not None:
                subprocess.run(
                    ("hermes", "sessions", "delete", "--yes", session_id),
                    cwd=tmp_path,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
