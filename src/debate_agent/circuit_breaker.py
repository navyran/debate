"""CLI 基础设施熔断记录与持久化。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from debate_agent.health_models import (
    CliIdentity,
    FailureCategory,
)


class CircuitRecord(BaseModel):
    """保存一个已打开或正在计数的基础设施熔断记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_hash: str
    category: FailureCategory
    signature_hash: str
    failures: tuple[datetime, ...]
    open_until: datetime | None = None


class CircuitBreakerStore:
    """持久化按 CLI 身份和错误指纹隔离的短窗口熔断状态。"""

    def __init__(
        self,
        *,
        state_root: Path,
        threshold: int,
        window_seconds: int,
        cooldown_seconds: int,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """创建熔断器。

        参数为状态根、阈值、窗口、冷却和时钟；返回实例；非正预算抛 ValueError。
        """
        if min(threshold, window_seconds, cooldown_seconds) <= 0:
            raise ValueError("熔断阈值和时间窗口必须为正数")
        self.path = state_root.expanduser().resolve() / "health" / "circuits.json"
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.now = now

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """获取熔断文件互斥锁；无参数，生成控制权；锁或 I/O 失败时抛异常。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a+") as handle:
            self._lock_file(handle)
            try:
                yield
            finally:
                self._unlock_file(handle)

    @staticmethod
    def _lock_file(handle: IO[str]) -> None:
        """锁定文件；参数为锁句柄，无返回值，不支持的平台退化为原子替换保护。"""
        try:
            import fcntl
        except ModuleNotFoundError:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock_file(handle: IO[str]) -> None:
        """释放文件锁；参数为锁句柄，无返回值，不支持的平台直接返回。"""
        try:
            import fcntl
        except ModuleNotFoundError:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _identity_hash(identity: CliIdentity) -> str:
        """生成身份摘要；参数为 CLI 身份，返回 SHA-256，不抛异常。"""
        return hashlib.sha256(identity.model_dump_json().encode("utf-8")).hexdigest()

    def _read(self) -> dict[str, CircuitRecord]:
        """读取全部熔断记录；无参数，返回记录映射，文件损坏时抛错。"""
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("circuits.json 顶层必须是对象")
        return {
            key: CircuitRecord.model_validate(value) for key, value in payload.items()
        }

    def _write(self, records: Mapping[str, CircuitRecord]) -> None:
        """原子写记录；参数为映射，无返回值，I/O 错误时抛异常。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(
                {
                    key: record.model_dump(mode="json")
                    for key, record in records.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def active(self, identity: CliIdentity) -> CircuitRecord | None:
        """读取当前身份的打开熔断。

        参数为 CLI 身份；返回尚未冷却的记录或 None；文件损坏时抛错。
        """
        identity_hash = self._identity_hash(identity)
        current = self.now()
        for record in self._read().values():
            if (
                record.identity_hash == identity_hash
                and record.open_until is not None
                and record.open_until > current
            ):
                return record
        return None

    def record_failure(
        self,
        identity: CliIdentity,
        *,
        category: FailureCategory,
        signature: str,
    ) -> CircuitRecord | None:
        """记录基础设施失败。

        参数为身份、类别和脱敏签名；返回新打开的记录或 None；文件错误时抛异常。
        """
        with self._locked():
            records = self._read()
            identity_hash = self._identity_hash(identity)
            signature_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()
            key = f"{identity_hash}:{category.value}:{signature_hash}"
            current = self.now()
            cutoff = current - timedelta(seconds=self.window_seconds)
            previous = records.get(key)
            failures = (
                tuple(
                    timestamp for timestamp in previous.failures if timestamp >= cutoff
                )
                if previous is not None
                else ()
            )
            failures = (*failures, current)
            open_until = (
                current + timedelta(seconds=self.cooldown_seconds)
                if len(failures) >= self.threshold
                else None
            )
            record = CircuitRecord(
                identity_hash=identity_hash,
                category=category,
                signature_hash=signature_hash,
                failures=failures,
                open_until=open_until,
            )
            records[key] = record
            self._write(records)
            return record if open_until is not None else None

    def record_success(self, identity: CliIdentity) -> None:
        """清除身份的历史失败；参数为身份，无返回值，文件错误时抛异常。"""
        with self._locked():
            records = self._read()
            identity_hash = self._identity_hash(identity)
            kept = {
                key: record
                for key, record in records.items()
                if record.identity_hash != identity_hash
            }
            self._write(kept)
