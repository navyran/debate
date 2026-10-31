"""为 Streamlit 等前端启动独立 Debate Worker。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.events import RunEventStore
from debate_agent.models import RunStatus
from debate_agent.runtime import DebateLifecycleService


class SpawnedProcess(Protocol):
    """声明启动器只依赖子进程 PID。"""

    pid: int


class LaunchResult(BaseModel):
    """描述页面启动或恢复 Worker 后可立即返回的结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    pid: int = Field(gt=0)
    status: str
    mode: str


class RunLauncher:
    """冻结配置并在独立进程中启动或恢复 Debate Run。"""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., SpawnedProcess] = subprocess.Popen,
        run_id_factory: Callable[[], str] = DebateLifecycleService.new_run_id,
    ) -> None:
        """创建启动器；参数为进程/run ID 工厂，返回实例，不执行外部动作。"""
        self.popen_factory = popen_factory
        self.run_id_factory = run_id_factory

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        """原子写入 Worker 记录；参数为路径和载荷，无返回值，I/O 失败时抛错。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _spawn(self, *, state_root: Path, run_id: str, mode: str) -> LaunchResult:
        """启动独立 Worker；参数为状态根/run/模式，返回启动结果，进程失败时抛错。"""
        store = ArtifactStore.open(state_root=state_root, run_id=run_id)
        events = RunEventStore(state_root=state_root, run_id=run_id)
        config = store.load_config()
        argv = [
            sys.executable,
            "-m",
            "debate_agent.worker",
            mode,
            "--run-id",
            run_id,
            "--state-root",
            str(state_root.expanduser().resolve()),
        ]
        log_path = store.run_dir / "worker.log"
        try:
            with log_path.open("ab") as log_handle:
                process = self.popen_factory(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            reason = f"Worker 启动失败: {str(exc)[:300]}"
            store.write_status(RunStatus.FAILED, stop_reason=reason)
            events.publish(
                kind="worker_failed",
                phase="stopped",
                title="Worker 启动失败",
                summary=reason,
                status=RunStatus.FAILED.value,
                max_turns=config.max_turns,
            )
            raise RuntimeError(reason) from exc
        self._atomic_json(
            store.run_dir / "worker.json",
            {
                "run_id": run_id,
                "pid": process.pid,
                "mode": mode,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return LaunchResult(
            run_id=run_id,
            pid=process.pid,
            status=RunStatus.RUNNING.value,
            mode=mode,
        )

    def start(self, config: RunConfig) -> LaunchResult:
        """创建并启动新 run；参数为冻结配置，返回启动结果，持久化或进程失败时抛错。"""
        run_id = self.run_id_factory()
        ArtifactStore.create(config, run_id=run_id)
        RunEventStore(state_root=config.state_root, run_id=run_id).publish(
            kind="run_queued",
            phase="queued",
            title="讨论任务已进入执行队列",
            current_turn=0,
            max_turns=config.max_turns,
        )
        return self._spawn(state_root=config.state_root, run_id=run_id, mode="run")

    def resume(self, *, state_root: Path, run_id: str) -> LaunchResult:
        """在独立 Worker 中恢复 run。

        参数为状态根/run，返回启动结果，运行不存在时抛错。
        """
        store = ArtifactStore.open(state_root=state_root, run_id=run_id)
        status = str(store.read_status().get("status", "unknown"))
        if status == RunStatus.RUNNING.value:
            raise RuntimeError("该 run 正在运行，不能重复恢复")
        if status == RunStatus.COMPLETED.value:
            raise RuntimeError("该 run 已完成，无需恢复")
        store.write_status(RunStatus.RUNNING, stop_reason=None)
        config = store.load_config()
        RunEventStore(state_root=state_root, run_id=run_id).publish(
            kind="run_queued",
            phase="recovery",
            title="恢复任务已进入执行队列",
            max_turns=config.max_turns,
        )
        return self._spawn(state_root=state_root, run_id=run_id, mode="resume")
