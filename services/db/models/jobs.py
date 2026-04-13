"""
Purpose: Persist background-job lifecycle state, checkpoints, and failure metadata.
Scope: Durable async-job records for parsing, OCR, recommendations, reconciliation,
and reporting so operators can inspect, cancel, and resume work safely.
Dependencies: Shared job status enum, SQLAlchemy ORM primitives, PostgreSQL JSONB,
and related entity/close-run/document/auth tables.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from services.common.enums import JobStatus
from services.common.types import JsonObject
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Job(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one canonical asynchronous job lifecycle record."""

    __tablename__ = "jobs"
    __table_args__ = (
        build_text_choice_check(
            column_name="status",
            values=JobStatus.values(),
            constraint_name="status_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
        CheckConstraint("max_retries >= 0", name="max_retries_non_negative"),
        CheckConstraint("retry_count <= attempt_count", name="retry_count_within_attempts"),
        CheckConstraint(
            "attempt_count <= max_retries + 1",
            name="attempt_count_within_retry_budget",
        ),
        CheckConstraint(
            "(status = 'blocked' AND blocking_reason IS NOT NULL) "
            "OR (status <> 'blocked' AND blocking_reason IS NULL)",
            name="blocking_reason_matches_status",
        ),
        CheckConstraint(
            "dead_lettered_at IS NULL OR status = 'failed'",
            name="dead_letter_requires_failed_status",
        ),
        CheckConstraint(
            "canceled_at IS NULL OR status = 'canceled'",
            name="canceled_timestamp_requires_canceled_status",
        ),
        Index("ix_jobs_entity_id_status", "entity_id", "status"),
        Index("ix_jobs_close_run_id_status", "close_run_id", "status"),
        Index("ix_jobs_document_id_status", "document_id", "status"),
        Index("ix_jobs_task_name_status", "task_name", "status"),
    )

    entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    close_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("close_runs.id"), nullable=True)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    canceled_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    resumed_from_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    task_name: Mapped[str] = mapped_column(String(120), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(60), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    checkpoint_payload: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    result_payload: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_details: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    blocking_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    max_retries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(nullable=True)


__all__ = ["Job"]
