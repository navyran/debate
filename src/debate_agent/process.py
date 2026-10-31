"""受控 CLI 子进程执行与运行期原始输出持久化。"""

from __future__ import annotations

import codecs
import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Event
from typing import BinaryIO


class ProcessTermination(str, Enum):
    """定义一次 CLI 子进程的明确终止类型。"""

    COMPLETED = "completed"
    PROCESS_ERROR = "process_error"
    STARTUP_TIMEOUT = "startup_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    HARD_TIMEOUT = "hard_timeout"
    CANCELLED = "cancelled"


class PromptTransport(str, Enum):
    """定义 CLI Prompt 的传输通道。"""

    STDIN = "stdin"
    TEMP_FILE = "temp_file"
    ARGV = "argv"


class ArgvPromptTooLargeError(ValueError):
    """表示 Prompt 的 UTF-8 字节数超过安全 argv 上限。"""


@dataclass(frozen=True, slots=True)
class TemporaryInput:
    """描述一次调用需要在系统临时目录物化的敏感输入。"""

    marker: str
    content: str = field(repr=False)
    suffix: str = ".txt"

    def __post_init__(self) -> None:
        """校验临时输入。

        参数：使用实例字段中的替换标记、内容和后缀。
        返回值：无。
        异常：标记为空、内容不是文本或后缀含路径语义时抛 ValueError。
        """
        if not self.marker:
            raise ValueError("临时输入 marker 不能为空")
        if not isinstance(self.content, str):
            raise ValueError("临时输入 content 必须是文本")
        if re.fullmatch(r"(?:\.[A-Za-z0-9._-]+)?", self.suffix) is None:
            raise ValueError("临时输入 suffix 必须是安全文件后缀")


def validate_argv_prompt(prompt: str, *, max_bytes: int) -> str:
    """校验 argv Prompt 的 UTF-8 字节上限。

    参数：prompt 为完整文本，max_bytes 为允许的最大字节数。
    返回值：未修改、未截断的原始 Prompt。
    异常：上限非正时抛 ValueError，Prompt 超限时抛 ArgvPromptTooLargeError。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须为正数")
    actual = len(prompt.encode("utf-8"))
    if actual > max_bytes:
        raise ArgvPromptTooLargeError(
            f"argv Prompt 上限为 {max_bytes} 字节，实际为 {actual} 字节"
        )
    return prompt


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """保存一条增量解码后的 stdout 或 stderr 输出。"""

    stream: str
    text: str
    elapsed_seconds: float
    complete_line: bool


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """保存心跳回调使用的非敏感进程活动快照。"""

    elapsed_seconds: float
    idle_seconds: float
    useful_activity_count: int
    has_useful_activity: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """保存一次受控子进程的有限输出和运行期引用。"""

    return_code: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    raw_output_ref: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    cancelled: bool
    termination: ProcessTermination = ProcessTermination.COMPLETED
    last_activity_elapsed_seconds: float | None = None
    useful_activity_count: int = 0
    stdout_path: Path | None = None
    stderr_path: Path | None = None


class ProcessRunner:
    """以独立进程组运行 argv 命令，不把 Prompt 或环境写入普通日志。"""

    def __init__(self, *, raw_root: Path, max_output_bytes: int = 1_048_576) -> None:
        """创建进程执行器。

        参数为原始输出目录和内存输出上限；返回实例；上限非法时抛 ValueError。
        """
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须为正数")
        self.raw_root = raw_root.expanduser().resolve()
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        """终止整个进程组；参数为子进程，无返回值，已退出进程不会报错。"""
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()

    def _read_capped(self, path: Path) -> tuple[str, bool]:
        """读取有限输出；参数为文件，返回 UTF-8 文本与截断标记，I/O 失败时抛错。"""
        with path.open("rb") as handle:
            payload = handle.read(self.max_output_bytes + 1)
        truncated = len(payload) > self.max_output_bytes
        return payload[: self.max_output_bytes].decode(
            "utf-8", errors="replace"
        ), truncated

    @staticmethod
    def _pump_stream(
        *,
        stream_name: str,
        pipe: BinaryIO,
        raw_handle: BinaryIO,
        outputs: queue.Queue[ProcessOutput],
        started: float,
    ) -> None:
        """持续抽取一个管道。

        参数为流名、二进制管道、raw 文件、输出队列和开始时间；无返回值；
        解码使用替换策略，底层 I/O 异常会在线程结束时停止该流。
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        fileno = pipe.fileno()
        try:
            while True:
                chunk = os.read(fileno, 4096)
                if not chunk:
                    break
                raw_handle.write(chunk)
                raw_handle.flush()
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    outputs.put(
                        ProcessOutput(
                            stream=stream_name,
                            text=f"{line}\n",
                            elapsed_seconds=max(0.0, time.monotonic() - started),
                            complete_line=True,
                        )
                    )
            pending += decoder.decode(b"", final=True)
            if pending:
                outputs.put(
                    ProcessOutput(
                        stream=stream_name,
                        text=pending,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                        complete_line=False,
                    )
                )
        finally:
            pipe.close()

    @staticmethod
    def _pump_stdin(*, pipe: BinaryIO, payload: bytes) -> None:
        """异步写入 stdin。

        参数为二进制管道和 Prompt 字节；无返回值；子进程提前退出时忽略断管错误。
        """
        try:
            with suppress(BrokenPipeError, OSError):
                pipe.write(payload)
                pipe.flush()
        finally:
            with suppress(OSError):
                pipe.close()

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        """原子写 JSON；参数为路径和载荷，无返回值，文件错误时抛 OSError。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    @staticmethod
    def _materialize_temporary_inputs(
        command: tuple[str, ...],
        inputs: Sequence[TemporaryInput],
    ) -> tuple[tuple[str, ...], tempfile.TemporaryDirectory[str] | None]:
        """物化敏感临时输入并替换 argv 标记。

        参数：command 为未展开 argv，inputs 为敏感输入定义。
        返回值：替换后的 argv 和负责清理的临时目录。
        异常：标记缺失、重复、冲突或文件写入失败时抛 ValueError/OSError。
        """
        if not inputs:
            return command, None
        markers = [item.marker for item in inputs]
        if len(markers) != len(set(markers)):
            raise ValueError("临时输入 marker 不能重复")
        directory = tempfile.TemporaryDirectory(prefix="debate-agent-invocation-")
        materialized = command
        try:
            for index, item in enumerate(inputs):
                occurrences = sum(part.count(item.marker) for part in materialized)
                if occurrences != 1:
                    raise ValueError(
                        f"临时输入 marker 必须在 argv 中恰好出现一次: {item.marker}"
                    )
                path = Path(directory.name) / f"input-{index}{item.suffix}"
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        handle.write(item.content)
                except BaseException:
                    with suppress(OSError):
                        os.close(descriptor)
                    raise
                materialized = tuple(
                    part.replace(item.marker, str(path)) for part in materialized
                )
        except BaseException:
            directory.cleanup()
            raise
        return materialized, directory

    def run(
        self,
        *,
        invocation_id: str,
        argv: Sequence[str],
        audit_argv: Sequence[str] | None = None,
        temporary_inputs: Sequence[TemporaryInput] = (),
        cwd: Path,
        stdin: str | None,
        timeout_seconds: float,
        env: Mapping[str, str],
        cancel_event: Event | None = None,
        on_started: Callable[[int, int], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        startup_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
        hard_timeout_seconds: float | None = None,
        heartbeat_interval_seconds: float = 5.0,
        on_output: Callable[[ProcessOutput], None] | None = None,
        is_useful_activity: Callable[[ProcessOutput], bool] | None = None,
        on_heartbeat: Callable[[ProcessSnapshot], None] | None = None,
        workspace_fingerprint: Callable[[], str] | None = None,
        fingerprint_poll_seconds: float = 0.25,
        stdout_persist_transform: Callable[[str], str] | None = None,
    ) -> ProcessResult:
        """执行受控命令。

        参数为调用身份、进程输入、分层时限、活动/心跳回调和工作区指纹；返回结果；
        参数或文件错误时抛错，超时与取消会终止进程组并记录明确终止类型。
        """
        original_command = tuple(argv)
        if not original_command or any(not part for part in original_command):
            raise ValueError("argv 必须为非空字符串序列")
        audit_command = tuple(audit_argv) if audit_argv is not None else None
        if audit_command is not None and (
            not audit_command or any(not part for part in audit_command)
        ):
            raise ValueError("audit_argv 必须为非空字符串序列")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", invocation_id) is None:
            raise ValueError("invocation_id 必须是安全文件名标识")
        hard_timeout = (
            hard_timeout_seconds
            if hard_timeout_seconds is not None
            else timeout_seconds
        )
        startup_timeout = (
            startup_timeout_seconds
            if startup_timeout_seconds is not None
            else hard_timeout
        )
        idle_timeout = (
            idle_timeout_seconds
            if idle_timeout_seconds is not None
            else hard_timeout
        )
        if min(
            timeout_seconds,
            startup_timeout,
            idle_timeout,
            hard_timeout,
            heartbeat_interval_seconds,
            fingerprint_poll_seconds,
        ) <= 0:
            raise ValueError("进程时限和轮询间隔必须为正数")
        workdir = cwd.expanduser().resolve()
        if not workdir.is_dir():
            raise ValueError(f"cwd 不是目录: {workdir}")
        command, temporary_directory = self._materialize_temporary_inputs(
            original_command,
            temporary_inputs,
        )

        stdout_final = self.raw_root / f"{invocation_id}.stdout.log"
        stderr_final = self.raw_root / f"{invocation_id}.stderr.log"
        manifest = self.raw_root / f"{invocation_id}.json"
        stdout_fd: int | None = None
        stderr_fd: int | None = None
        stdout_temp: Path | None = None
        stderr_temp: Path | None = None
        try:
            stdout_fd, stdout_temp_text = tempfile.mkstemp(
                prefix=f".{invocation_id}.", suffix=".stdout.tmp", dir=self.raw_root
            )
            stdout_temp = Path(stdout_temp_text)
            stderr_fd, stderr_temp_text = tempfile.mkstemp(
                prefix=f".{invocation_id}.", suffix=".stderr.tmp", dir=self.raw_root
            )
            stderr_temp = Path(stderr_temp_text)
        except BaseException:
            for descriptor in (stdout_fd, stderr_fd):
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)
            for path in (stdout_temp, stderr_temp):
                if path is not None:
                    path.unlink(missing_ok=True)
            if temporary_directory is not None:
                temporary_directory.cleanup()
            raise
        assert stdout_fd is not None
        assert stderr_fd is not None
        assert stdout_temp is not None
        assert stderr_temp is not None
        started = time.monotonic()
        cancelled = False
        termination = ProcessTermination.COMPLETED
        process: subprocess.Popen[bytes] | None = None
        output_queue: queue.Queue[ProcessOutput] = queue.Queue()
        useful_activity_count = 0
        first_useful_at: float | None = None
        last_useful_at: float | None = None
        last_fingerprint = (
            workspace_fingerprint() if workspace_fingerprint is not None else None
        )
        next_fingerprint_at = started + fingerprint_poll_seconds
        next_heartbeat_at = started + heartbeat_interval_seconds

        def consume_outputs() -> None:
            """消费增量输出；无参数和返回值；用户回调异常会传播到主执行路径。"""
            nonlocal useful_activity_count, first_useful_at, last_useful_at
            while True:
                try:
                    output = output_queue.get_nowait()
                except queue.Empty:
                    return
                if on_output is not None:
                    on_output(output)
                useful = (
                    is_useful_activity(output)
                    if is_useful_activity is not None
                    else bool(output.text.strip())
                )
                if useful:
                    observed_at = started + output.elapsed_seconds
                    first_useful_at = first_useful_at or observed_at
                    last_useful_at = observed_at
                    useful_activity_count += 1

        try:
            with (
                os.fdopen(stdout_fd, "wb") as stdout_handle,
                os.fdopen(stderr_fd, "wb") as stderr_handle,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=workdir,
                    stdin=(
                        subprocess.PIPE
                        if stdin is not None
                        else subprocess.DEVNULL
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=dict(env),
                    shell=False,
                    start_new_session=os.name == "posix",
                )
                if on_started is not None:
                    process_group = process.pid if os.name == "posix" else process.pid
                    on_started(process.pid, process_group)
                assert process.stdout is not None
                assert process.stderr is not None
                stdout_thread = threading.Thread(
                    target=self._pump_stream,
                    kwargs={
                        "stream_name": "stdout",
                        "pipe": process.stdout,
                        "raw_handle": stdout_handle,
                        "outputs": output_queue,
                        "started": started,
                    },
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._pump_stream,
                    kwargs={
                        "stream_name": "stderr",
                        "pipe": process.stderr,
                        "raw_handle": stderr_handle,
                        "outputs": output_queue,
                        "started": started,
                    },
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                stdin_thread: threading.Thread | None = None
                if stdin is not None:
                    assert process.stdin is not None
                    stdin_thread = threading.Thread(
                        target=self._pump_stdin,
                        kwargs={
                            "pipe": process.stdin,
                            "payload": stdin.encode("utf-8"),
                        },
                        daemon=True,
                    )
                    stdin_thread.start()
                while process.poll() is None:
                    consume_outputs()
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        termination = ProcessTermination.CANCELLED
                        self._terminate_process_group(process)
                        break
                    if cancel_requested is not None and cancel_requested():
                        cancelled = True
                        termination = ProcessTermination.CANCELLED
                        self._terminate_process_group(process)
                        break
                    now = time.monotonic()
                    if (
                        workspace_fingerprint is not None
                        and now >= next_fingerprint_at
                    ):
                        current_fingerprint = workspace_fingerprint()
                        next_fingerprint_at = now + fingerprint_poll_seconds
                        if current_fingerprint != last_fingerprint:
                            last_fingerprint = current_fingerprint
                            first_useful_at = first_useful_at or now
                            last_useful_at = now
                            useful_activity_count += 1
                    elapsed_now = now - started
                    if elapsed_now >= hard_timeout:
                        termination = ProcessTermination.HARD_TIMEOUT
                        self._terminate_process_group(process)
                        break
                    if (
                        first_useful_at is None
                        and elapsed_now >= startup_timeout
                    ):
                        termination = ProcessTermination.STARTUP_TIMEOUT
                        self._terminate_process_group(process)
                        break
                    if (
                        last_useful_at is not None
                        and now - last_useful_at >= idle_timeout
                    ):
                        termination = ProcessTermination.IDLE_TIMEOUT
                        self._terminate_process_group(process)
                        break
                    if on_heartbeat is not None and now >= next_heartbeat_at:
                        on_heartbeat(
                            ProcessSnapshot(
                                elapsed_seconds=elapsed_now,
                                idle_seconds=(
                                    now - last_useful_at
                                    if last_useful_at is not None
                                    else elapsed_now
                                ),
                                useful_activity_count=useful_activity_count,
                                has_useful_activity=first_useful_at is not None,
                            )
                        )
                        next_heartbeat_at = now + heartbeat_interval_seconds
                    time.sleep(0.005)
                process.wait()
                if stdin_thread is not None:
                    stdin_thread.join()
                stdout_thread.join()
                stderr_thread.join()
                consume_outputs()
            if stdout_persist_transform is not None:
                sanitized = stdout_persist_transform(
                    stdout_temp.read_text(encoding="utf-8", errors="replace")
                )
                stdout_temp.write_text(sanitized, encoding="utf-8")
            os.replace(stdout_temp, stdout_final)
            os.replace(stderr_temp, stderr_final)
        except BaseException:
            if process is not None:
                self._terminate_process_group(process)
            stdout_temp.unlink(missing_ok=True)
            stderr_temp.unlink(missing_ok=True)
            if temporary_directory is not None:
                temporary_directory.cleanup()
            raise
        if temporary_directory is not None:
            temporary_directory.cleanup()

        elapsed = time.monotonic() - started
        stdout, stdout_truncated = self._read_capped(stdout_final)
        stderr, stderr_truncated = self._read_capped(stderr_final)
        return_code = process.returncode if process is not None else None
        if termination is ProcessTermination.COMPLETED and return_code != 0:
            termination = ProcessTermination.PROCESS_ERROR
        timed_out = termination in {
            ProcessTermination.STARTUP_TIMEOUT,
            ProcessTermination.IDLE_TIMEOUT,
            ProcessTermination.HARD_TIMEOUT,
        }
        last_activity_elapsed = (
            max(0.0, last_useful_at - started)
            if last_useful_at is not None
            else None
        )
        manifest_payload: dict[str, object] = {
            "invocation_id": invocation_id,
            "return_code": return_code,
            "elapsed_seconds": elapsed,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "termination": termination.value,
            "last_activity_elapsed_seconds": last_activity_elapsed,
            "useful_activity_count": useful_activity_count,
            "stdout_file": stdout_final.name,
            "stderr_file": stderr_final.name,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        if audit_command is not None:
            manifest_payload["argv"] = list(audit_command)
        self._atomic_json(manifest, manifest_payload)
        raw_output_ref = manifest.relative_to(self.raw_root.parent).as_posix()
        return ProcessResult(
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=elapsed,
            raw_output_ref=raw_output_ref,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
            cancelled=cancelled,
            termination=termination,
            last_activity_elapsed_seconds=last_activity_elapsed,
            useful_activity_count=useful_activity_count,
            stdout_path=stdout_final,
            stderr_path=stderr_final,
        )

    @staticmethod
    def wait_for_quiescence(
        *,
        fingerprint: Callable[[], str],
        quiet_seconds: float,
        timeout_seconds: float,
        poll_seconds: float = 0.05,
    ) -> str:
        """等待工作树指纹稳定。

        参数为指纹函数、静默窗口、总时限和轮询间隔；返回稳定指纹；超时抛错。
        """
        if min(quiet_seconds, timeout_seconds, poll_seconds) <= 0:
            raise ValueError("静默窗口、总时限和轮询间隔必须为正数")
        deadline = time.monotonic() + timeout_seconds
        last = fingerprint()
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            current = fingerprint()
            now = time.monotonic()
            if current != last:
                last = current
                stable_since = now
                continue
            if now - stable_since >= quiet_seconds:
                return current
        raise TimeoutError("工作树未在限定时间内达到静默状态")
