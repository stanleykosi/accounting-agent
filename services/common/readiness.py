"""
Purpose: Track canonical backend dependency readiness for long-lived hosted processes.
Scope: In-memory API readiness snapshots used by startup probes, route gating, and status routes.
Dependencies: Thread-safe state updates and shared UTC timestamp helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Literal

from services.common.types import utc_now

BackendDependencyReadinessStatus = Literal["starting", "retrying", "ready", "failed"]


@dataclass(frozen=True)
class BackendDependencyReadinessSnapshot:
    """Describe the current backend dependency readiness state for one process."""

    attempt_count: int
    last_checked_at: datetime | None
    last_error: str | None
    ready: bool
    status: BackendDependencyReadinessStatus


class BackendDependencyReadiness:
    """Store the canonical in-process readiness state for dependency-backed API routes."""

    def __init__(self) -> None:
        self._attempt_count = 0
        self._last_checked_at: datetime | None = None
        self._last_error: str | None = None
        self._ready = False
        self._status: BackendDependencyReadinessStatus = "starting"
        self._lock = Lock()

    def mark_ready(self, *, attempt_count: int) -> None:
        """Record that the full dependency probe completed successfully."""

        with self._lock:
            self._attempt_count = attempt_count
            self._last_checked_at = utc_now()
            self._last_error = None
            self._ready = True
            self._status = "ready"

    def mark_retrying(self, *, attempt_count: int, error_message: str) -> None:
        """Record a failed probe attempt while keeping the process alive for the next retry."""

        with self._lock:
            self._attempt_count = attempt_count
            self._last_checked_at = utc_now()
            self._last_error = error_message
            self._ready = False
            self._status = "retrying"

    def mark_failed(self, *, attempt_count: int, error_message: str) -> None:
        """Record a non-retryable dependency failure for operator diagnostics."""

        with self._lock:
            self._attempt_count = attempt_count
            self._last_checked_at = utc_now()
            self._last_error = error_message
            self._ready = False
            self._status = "failed"

    def snapshot(self) -> BackendDependencyReadinessSnapshot:
        """Return the current immutable readiness snapshot."""

        with self._lock:
            return BackendDependencyReadinessSnapshot(
                attempt_count=self._attempt_count,
                last_checked_at=self._last_checked_at,
                last_error=self._last_error,
                ready=self._ready,
                status=self._status,
            )
