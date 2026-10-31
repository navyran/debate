from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.events import RunEventStore
from debate_agent.launcher import RunLauncher
from debate_agent.models import RunStatus
from debate_agent.worker import main as worker_main


class FakeProcess:
    """模拟已成功启动的独立 Worker 进程。"""

    pid = 4321


class RecordingPopen:
    """记录启动参数并返回固定 PID。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        """记录进程调用；参数为 argv/选项，返回假进程，不抛异常。"""
        self.calls.append((argv, kwargs))
        return FakeProcess()


class FakeWorkerLifecycle:
    """记录 Worker 对 Debate 生命周期的调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run_with_id(
        self, config: RunConfig, *, run_id: str
    ) -> dict[str, object]:
        """记录新运行；参数为配置/run，返回完成状态。"""
        self.calls.append(("run", run_id))
        return {"run_id": run_id, "status": RunStatus.COMPLETED.value}

    def resume(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """记录恢复；参数为状态根/run，返回完成状态。"""
        self.calls.append(("resume", run_id))
        return {"run_id": run_id, "status": RunStatus.COMPLETED.value}


class UnexpectedFailureLifecycle(FakeWorkerLifecycle):
    """模拟生命周期抛出 Worker 必须兜底记录的未预期异常。"""

    def run_with_id(
        self, config: RunConfig, *, run_id: str
    ) -> dict[str, object]:
        """抛出未预期异常；参数为配置/run，不返回结果。"""
        raise IndexError("simulated unexpected failure")


def test_launcher_persists_config_returns_run_id_and_spawns_worker(
    tmp_path: Path,
) -> None:
    popen = RecordingPopen()
    config = RunConfig(
        workspace=tmp_path,
        topic="实现页面入口",
        state_root=tmp_path / "state",
    )
    launcher = RunLauncher(
        popen_factory=popen,
        run_id_factory=lambda: "RUN-UI-1",
    )

    result = launcher.start(config)

    assert result.run_id == "RUN-UI-1"
    assert result.pid == 4321
    assert result.status == RunStatus.RUNNING.value
    stored = ArtifactStore.open(
        state_root=config.state_root, run_id="RUN-UI-1"
    ).load_config()
    assert stored.topic == "实现页面入口"
    argv, options = popen.calls[0]
    assert argv == [
        sys.executable,
        "-m",
        "debate_agent.worker",
        "run",
        "--run-id",
        "RUN-UI-1",
        "--state-root",
        str(config.state_root),
    ]
    assert options["start_new_session"] is True
    record = json.loads(
        (config.state_root / "runs" / "RUN-UI-1" / "worker.json").read_text()
    )
    assert record["pid"] == 4321
    assert record["mode"] == "run"
    events = RunEventStore(
        state_root=config.state_root, run_id="RUN-UI-1"
    ).read_events()
    assert events[0].kind == "run_queued"


def test_worker_uses_preallocated_run_and_can_resume(tmp_path: Path) -> None:
    config = RunConfig(
        workspace=tmp_path,
        topic="后台运行",
        state_root=tmp_path / "state",
    )
    ArtifactStore.create(config, run_id="RUN-UI-2")
    lifecycle = FakeWorkerLifecycle()
    args = [
        "--run-id",
        "RUN-UI-2",
        "--state-root",
        str(config.state_root),
    ]

    assert worker_main(["run", *args], lifecycle=lifecycle) == 0
    assert worker_main(["resume", *args], lifecycle=lifecycle) == 0

    assert lifecycle.calls == [("run", "RUN-UI-2"), ("resume", "RUN-UI-2")]


def test_worker_marks_unexpected_exception_as_failed(tmp_path: Path) -> None:
    """验证 Worker 未预期异常也会落失败状态，避免页面永久显示运行中。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="后台异常",
        state_root=tmp_path / "state",
    )
    store = ArtifactStore.create(config, run_id="RUN-WORKER-FAILED")
    args = [
        "--run-id",
        "RUN-WORKER-FAILED",
        "--state-root",
        str(config.state_root),
    ]

    code = worker_main(
        ["run", *args], lifecycle=UnexpectedFailureLifecycle()
    )

    assert code == 2
    status = store.read_status()
    assert status["status"] == RunStatus.FAILED.value
    assert status["stop_reason"] == "IndexError: simulated unexpected failure"
    events = RunEventStore(
        state_root=config.state_root, run_id="RUN-WORKER-FAILED"
    ).read_events()
    assert events[-1].kind == "worker_failed"


def test_resume_marks_run_active_before_spawn_and_rejects_duplicate(
    tmp_path: Path,
) -> None:
    popen = RecordingPopen()
    config = RunConfig(
        workspace=tmp_path,
        topic="恢复运行",
        state_root=tmp_path / "state",
    )
    store = ArtifactStore.create(config, run_id="RUN-UI-3")
    store.write_status(RunStatus.BLOCKED, stop_reason="等待恢复")
    launcher = RunLauncher(popen_factory=popen)

    result = launcher.resume(state_root=config.state_root, run_id="RUN-UI-3")

    assert result.mode == "resume"
    assert store.read_status()["status"] == RunStatus.RUNNING.value
    with pytest.raises(RuntimeError, match="正在运行"):
        launcher.resume(state_root=config.state_root, run_id="RUN-UI-3")
    assert len(popen.calls) == 1
