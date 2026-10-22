from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


RunRegistryStatus = Literal["running", "completed", "cancelled", "failed"]


class RunCancelledError(Exception):
    """Raised when an agent run observes a cancellation request."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RunRecord:
    run_id: str
    conversation_id: str
    status: RunRegistryStatus = "running"
    started_at: datetime = field(default_factory=_now)
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_at: datetime | None = None
    reason: str | None = None
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set() or self.status == "cancelled"

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise RunCancelledError(f"Run {self.run_id} was cancelled")


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def start(self, conversation_id: str, run_id: str | None = None) -> RunRecord:
        resolved_run_id = run_id or str(uuid.uuid4())
        with self._lock:
            record = RunRecord(run_id=resolved_run_id, conversation_id=conversation_id)
            self._runs[resolved_run_id] = record
            return record

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def cancel(self, run_id: str, reason: str | None = None) -> RunRecord | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.status == "completed":
                return record
            record.status = "cancelled"
            record.reason = reason
            record.cancelled_at = record.cancelled_at or _now()
            record._cancel_event.set()
            return record

    def complete(self, run_id: str) -> RunRecord | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.status == "cancelled":
                return record
            record.status = "completed"
            record.completed_at = _now()
            return record

    def fail(self, run_id: str) -> RunRecord | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.status == "cancelled":
                return record
            record.status = "failed"
            record.failed_at = _now()
            return record
