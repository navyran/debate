from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event

import pytest

from debate_agent import process as process_module
from debate_agent.process import (
    ProcessOutput,
    ProcessRunner,
    ProcessTermination,
)


def test_process_runner_uses_stdin_and_persists_raw_output_without_prompt(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw", max_output_bytes=1024)
    secret_prompt = "PROMPT_SECRET_SHOULD_NOT_BE_IN_MANIFEST"

    result = runner.run(
        invocation_id="INV-1",
        argv=[
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.stdin.read()",
        ],
        cwd=tmp_path,
        stdin=secret_prompt,
        timeout_seconds=5,
        env={"PATH": "/usr/bin"},
    )

    assert result.return_code == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    manifest = (tmp_path / result.raw_output_ref).read_text(encoding="utf-8")
    assert secret_prompt not in manifest
    assert json.loads(manifest)["return_code"] == 0


def test_process_runner_does_not_persist_argv_without_explicit_audit_copy(
    tmp_path: Path,
) -> None:
    """验证普通调用不会把未知 argv 写入清单；参数为临时目录，无返回值。"""
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    secret_argument = "ARGV_SECRET_SHOULD_NOT_BE_PERSISTED"

    result = runner.run(
        invocation_id="INV-NO-AUDIT-ARGV",
        argv=(sys.executable, "-c", "pass", secret_argument),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=5,
        env={},
    )

    manifest_text = (tmp_path / result.raw_output_ref).read_text(encoding="utf-8")
    assert secret_argument not in manifest_text
    assert "argv" not in json.loads(manifest_text)


def test_process_runner_sanitizes_stdout_before_final_persistence(
    tmp_path: Path,
) -> None:
    """验证 stdout 通过调用级过滤器后才进入最终日志；参数为目录，无返回值。"""
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    secret = "OPENCLAW_FINAL_PROMPT_SECRET"

    result = runner.run(
        invocation_id="INV-SANITIZE-STDOUT",
        argv=(sys.executable, "-c", f"print({secret!r})"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=5,
        env={},
        stdout_persist_transform=lambda text: text.replace(secret, "<redacted>"),
    )

    persisted = result.stdout_path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert secret not in result.stdout
    assert persisted == "<redacted>\n"


def test_process_runner_materializes_secure_temporary_input_and_cleans_it(
    tmp_path: Path,
) -> None:
    """验证临时输入权限、内容、审计脱敏和退出清理；参数为临时目录，无返回值。"""
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    secret_prompt = "TEMP_PROMPT_SECRET"
    marker = "__DEBATE_PROMPT_FILE__"
    script = (
        "import hashlib, pathlib, stat, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "payload = path.read_bytes()\n"
        "mode = stat.S_IMODE(path.stat().st_mode)\n"
        "print(path)\n"
        "print(mode)\n"
        "print(hashlib.sha256(payload).hexdigest())\n"
    )
    temporary_input = process_module.TemporaryInput(
        marker=marker,
        content=secret_prompt,
        suffix=".md",
    )
    assert secret_prompt not in repr(temporary_input)

    result = runner.run(
        invocation_id="INV-TEMP-INPUT",
        argv=(sys.executable, "-c", script, marker),
        audit_argv=(sys.executable, "-c", "<script>", "<temporary-file>"),
        temporary_inputs=(temporary_input,),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=5,
        env={},
    )

    path_text, mode_text, digest = result.stdout.splitlines()
    temporary_path = Path(path_text)
    assert mode_text == str(0o600)
    assert digest == __import__("hashlib").sha256(secret_prompt.encode()).hexdigest()
    assert temporary_path.exists() is False
    manifest = json.loads((tmp_path / result.raw_output_ref).read_text())
    assert manifest["argv"][-1] == "<temporary-file>"
    assert secret_prompt not in json.dumps(manifest)
    assert secret_prompt not in result.stderr


@pytest.mark.parametrize(
    ("invocation_id", "script", "cancelled", "expected_termination"),
    (
        (
            "INV-TEMP-ERROR",
            "import pathlib,sys; print(sys.argv[1], flush=True); raise SystemExit(7)",
            False,
            ProcessTermination.PROCESS_ERROR,
        ),
        (
            "INV-TEMP-TIMEOUT",
            "import pathlib,sys,time; print(sys.argv[1], flush=True); time.sleep(2)",
            False,
            ProcessTermination.HARD_TIMEOUT,
        ),
        (
            "INV-TEMP-CANCEL",
            "import pathlib,sys,time; print(sys.argv[1], flush=True); time.sleep(2)",
            True,
            ProcessTermination.CANCELLED,
        ),
    ),
)
def test_process_runner_cleans_temporary_input_on_non_success_termination(
    tmp_path: Path,
    invocation_id: str,
    script: str,
    cancelled: bool,
    expected_termination: ProcessTermination,
) -> None:
    """验证非成功终止也清理临时输入；参数为场景值，无返回值。"""
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    cancel_event = Event()
    if cancelled:
        cancel_event.set()

    result = runner.run(
        invocation_id=invocation_id,
        argv=(sys.executable, "-u", "-c", script, "__INPUT__"),
        audit_argv=(sys.executable, "-u", "-c", "<script>", "<temporary-file>"),
        temporary_inputs=(
            process_module.TemporaryInput(
                marker="__INPUT__",
                content="secret",
                suffix=".txt",
            ),
        ),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        startup_timeout_seconds=0.2,
        idle_timeout_seconds=0.2,
        hard_timeout_seconds=0.05 if not cancelled else 1,
        heartbeat_interval_seconds=0.01,
        env={},
        cancel_event=cancel_event,
    )

    if result.stdout.strip():
        assert Path(result.stdout.strip()).exists() is False
    assert result.termination is expected_termination


@pytest.mark.parametrize(
    ("invocation_id", "useful", "expected_termination"),
    (
        ("INV-TEMP-STARTUP", False, ProcessTermination.STARTUP_TIMEOUT),
        ("INV-TEMP-IDLE", True, ProcessTermination.IDLE_TIMEOUT),
    ),
)
def test_process_runner_cleans_temporary_input_on_layered_timeout(
    tmp_path: Path,
    invocation_id: str,
    useful: bool,
    expected_termination: ProcessTermination,
) -> None:
    """验证启动/空闲超时清理临时输入；参数为场景值，无返回值。"""
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    result = runner.run(
        invocation_id=invocation_id,
        argv=(
            sys.executable,
            "-u",
            "-c",
            "import sys,time; print(sys.argv[1], flush=True); time.sleep(2)",
            "__INPUT__",
        ),
        audit_argv=(sys.executable, "-u", "-c", "<script>", "<temporary-file>"),
        temporary_inputs=(
            process_module.TemporaryInput(
                marker="__INPUT__",
                content="secret",
            ),
        ),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        startup_timeout_seconds=0.03,
        idle_timeout_seconds=0.03,
        hard_timeout_seconds=0.2,
        heartbeat_interval_seconds=0.01,
        env={},
        is_useful_activity=lambda _output: useful,
    )

    if result.stdout.strip():
        assert Path(result.stdout.strip()).exists() is False
    assert result.termination is expected_termination


def test_process_runner_cleans_temporary_input_when_callback_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证启动回调异常时清理临时输入；参数为临时目录/补丁器，无返回值。"""
    created: list[Path] = []
    real_temporary_directory = process_module.tempfile.TemporaryDirectory

    def tracked_temporary_directory(
        *args: object, **kwargs: object
    ) -> process_module.tempfile.TemporaryDirectory[str]:
        """记录临时目录；参数透传给标准库，返回目录对象，创建失败时抛 OSError。"""
        directory = real_temporary_directory(*args, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        process_module.tempfile,
        "TemporaryDirectory",
        tracked_temporary_directory,
    )
    runner = ProcessRunner(raw_root=tmp_path / "raw")

    with pytest.raises(RuntimeError, match="callback failed"):
        runner.run(
            invocation_id="INV-TEMP-CALLBACK",
            argv=(sys.executable, "-c", "pass", "__INPUT__"),
            temporary_inputs=(
                process_module.TemporaryInput(
                    marker="__INPUT__",
                    content="secret",
                ),
            ),
            cwd=tmp_path,
            stdin=None,
            timeout_seconds=1,
            env={},
            on_started=lambda _pid, _group: (_ for _ in ()).throw(
                RuntimeError("callback failed")
            ),
        )

    assert created
    assert all(path.exists() is False for path in created)


def test_process_runner_cleans_first_output_temp_when_second_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证第二个输出临时文件创建失败时清理首个文件；参数为目录/补丁器，无返回值。"""
    real_mkstemp = process_module.tempfile.mkstemp
    calls = 0

    def fail_second_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        """模拟第二次创建失败；参数透传，返回首个文件，第二次抛 OSError。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("stderr temp failed")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(process_module.tempfile, "mkstemp", fail_second_mkstemp)
    runner = ProcessRunner(raw_root=tmp_path / "raw")

    with pytest.raises(OSError, match="stderr temp failed"):
        runner.run(
            invocation_id="INV-TEMP-CREATE-FAIL",
            argv=(sys.executable, "-c", "pass"),
            cwd=tmp_path,
            stdin=None,
            timeout_seconds=1,
            env={},
        )

    assert list((tmp_path / "raw").iterdir()) == []


def test_argv_prompt_validator_rejects_utf8_overflow_without_truncation() -> None:
    """验证 argv Prompt 按 UTF-8 字节拒绝超限；无参数，无返回值。"""
    assert process_module.validate_argv_prompt("中文", max_bytes=6) == "中文"
    with pytest.raises(process_module.ArgvPromptTooLargeError, match="6.*7"):
        process_module.validate_argv_prompt("中文a", max_bytes=6)
    with pytest.raises(ValueError, match="max_bytes"):
        process_module.validate_argv_prompt("x", max_bytes=0)


def test_process_runner_caps_output_and_kills_timed_out_process(tmp_path: Path) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw", max_output_bytes=32)
    large = runner.run(
        invocation_id="INV-LARGE",
        argv=[sys.executable, "-c", "print('x' * 1000)"],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=5,
        env={},
    )
    timed_out = runner.run(
        invocation_id="INV-TIMEOUT",
        argv=[sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=0.05,
        env={},
    )

    assert large.stdout_truncated is True
    assert len(large.stdout.encode()) <= 32
    assert timed_out.timed_out is True
    assert timed_out.return_code is not None


def test_process_runner_honors_cancellation_and_waits_for_quiet_fingerprint(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    cancelled = Event()
    cancelled.set()

    result = runner.run(
        invocation_id="INV-CANCEL",
        argv=[sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=5,
        env={},
        cancel_event=cancelled,
    )
    values = iter(["before", "changed", "stable", "stable", "stable"])
    last = "stable"

    def fingerprint() -> str:
        nonlocal last
        last = next(values, last)
        return last

    quiet = runner.wait_for_quiescence(
        fingerprint=fingerprint,
        quiet_seconds=0.01,
        timeout_seconds=1,
        poll_seconds=0.005,
    )

    assert result.cancelled is True
    assert quiet == "stable"


def test_process_runner_handles_early_exit_and_rejects_unsafe_invocation_id(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")

    result = runner.run(
        invocation_id="INV-EARLY",
        argv=[sys.executable, "-c", "raise SystemExit(7)"],
        cwd=tmp_path,
        stdin="x" * 1_000_000,
        timeout_seconds=5,
        env={},
    )

    assert result.return_code == 7
    with pytest.raises(ValueError, match="invocation_id"):
        runner.run(
            invocation_id="../escape",
            argv=[sys.executable, "-c", "print('bad')"],
            cwd=tmp_path,
            stdin="",
            timeout_seconds=5,
            env={},
        )


def test_activity_aware_timeout_allows_useful_output_past_legacy_boundary(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    outputs: list[ProcessOutput] = []

    result = runner.run(
        invocation_id="INV-ACTIVE",
        argv=[
            sys.executable,
            "-u",
            "-c",
            (
                "import time\n"
                "for index in range(7):\n"
                " print(f'useful-{index}', flush=True)\n"
                " time.sleep(0.03)\n"
            ),
        ],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=0.05,
        startup_timeout_seconds=0.08,
        idle_timeout_seconds=0.08,
        hard_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.02,
        env={},
        on_output=outputs.append,
        is_useful_activity=lambda output: output.text.startswith("useful-"),
    )

    assert result.return_code == 0
    assert result.termination is ProcessTermination.COMPLETED
    assert result.elapsed_seconds > 0.05
    assert result.useful_activity_count == 7
    assert [output.text.strip() for output in outputs] == [
        f"useful-{index}" for index in range(7)
    ]


def test_process_runner_distinguishes_startup_idle_and_hard_timeouts(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    startup = runner.run(
        invocation_id="INV-STARTUP",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('thread.started', flush=True); time.sleep(1)",
        ],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=1,
        startup_timeout_seconds=0.05,
        idle_timeout_seconds=0.2,
        hard_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.02,
        env={},
        is_useful_activity=lambda output: output.text != "thread.started\n",
    )
    idle = runner.run(
        invocation_id="INV-IDLE",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('useful', flush=True); time.sleep(1)",
        ],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=1,
        startup_timeout_seconds=0.1,
        idle_timeout_seconds=0.05,
        hard_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.02,
        env={},
        is_useful_activity=lambda output: output.text == "useful\n",
    )
    hard = runner.run(
        invocation_id="INV-HARD",
        argv=[
            sys.executable,
            "-u",
            "-c",
            (
                "import time\n"
                "while True:\n"
                " print('useful', flush=True)\n"
                " time.sleep(0.01)\n"
            ),
        ],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=1,
        startup_timeout_seconds=0.1,
        idle_timeout_seconds=0.1,
        hard_timeout_seconds=0.08,
        heartbeat_interval_seconds=0.02,
        env={},
        is_useful_activity=lambda output: output.text == "useful\n",
    )

    assert startup.termination is ProcessTermination.STARTUP_TIMEOUT
    assert startup.useful_activity_count == 0
    assert idle.termination is ProcessTermination.IDLE_TIMEOUT
    assert idle.useful_activity_count == 1
    assert hard.termination is ProcessTermination.HARD_TIMEOUT
    assert hard.useful_activity_count >= 2
    assert startup.timed_out and idle.timed_out and hard.timed_out


def test_repeated_errors_do_not_refresh_activity_and_heartbeats_are_snapshots(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    heartbeats = []

    result = runner.run(
        invocation_id="INV-REPEATED",
        argv=[
            sys.executable,
            "-u",
            "-c",
            (
                "import time\n"
                "for _ in range(20):\n"
                " print('model cache incompatible', flush=True)\n"
                " time.sleep(0.01)\n"
            ),
        ],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=1,
        startup_timeout_seconds=0.06,
        idle_timeout_seconds=0.1,
        hard_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.02,
        env={},
        is_useful_activity=lambda _output: False,
        on_heartbeat=heartbeats.append,
    )

    assert result.termination is ProcessTermination.STARTUP_TIMEOUT
    assert len(heartbeats) >= 2
    assert all(snapshot.useful_activity_count == 0 for snapshot in heartbeats)


def test_workspace_fingerprint_change_counts_as_useful_activity(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    calls = 0

    def fingerprint() -> str:
        """模拟一次工作区变化；无参数，返回指纹，不抛异常。"""
        nonlocal calls
        calls += 1
        return "before" if calls < 3 else "after"

    result = runner.run(
        invocation_id="INV-FINGERPRINT",
        argv=[sys.executable, "-c", "import time; time.sleep(0.08)"],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=1,
        startup_timeout_seconds=0.03,
        idle_timeout_seconds=0.1,
        hard_timeout_seconds=0.3,
        heartbeat_interval_seconds=0.01,
        env={},
        workspace_fingerprint=fingerprint,
        fingerprint_poll_seconds=0.005,
    )

    assert result.termination is ProcessTermination.COMPLETED
    assert result.useful_activity_count == 1


def test_incremental_decoder_preserves_utf8_and_partial_final_line(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")
    outputs: list[ProcessOutput] = []

    result = runner.run(
        invocation_id="INV-UTF8",
        argv=[
            sys.executable,
            "-u",
            "-c",
            (
                "import os, sys, time\n"
                "data='中文'.encode()\n"
                "for byte in data:\n"
                " os.write(sys.stdout.fileno(), bytes([byte]))\n"
                " time.sleep(0.002)\n"
            ),
        ],
        cwd=tmp_path,
        stdin="",
        timeout_seconds=1,
        startup_timeout_seconds=0.1,
        idle_timeout_seconds=0.1,
        hard_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.02,
        env={},
        on_output=outputs.append,
    )

    assert result.stdout == "中文"
    assert len(outputs) == 1
    assert outputs[0].text == "中文"
    assert outputs[0].complete_line is False


def test_large_unread_stdin_does_not_block_timeout_monitor(tmp_path: Path) -> None:
    runner = ProcessRunner(raw_root=tmp_path / "raw")

    result = runner.run(
        invocation_id="INV-STDIN",
        argv=[sys.executable, "-c", "import time; time.sleep(1)"],
        cwd=tmp_path,
        stdin="x" * 2_000_000,
        timeout_seconds=1,
        startup_timeout_seconds=0.05,
        idle_timeout_seconds=0.1,
        hard_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.02,
        env={},
    )

    assert result.termination is ProcessTermination.STARTUP_TIMEOUT
    assert result.elapsed_seconds < 0.5
