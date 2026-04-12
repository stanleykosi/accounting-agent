"""
Purpose: Define recommendation and journal-entry persistence models.
Scope: Accounting recommendations with versioned payloads, evidence links, and review
lifecycle tracking. This is the minimal model needed for Step 27 recommendation generation;
Step 28 extends it with journal-entry materialization and approval routing.
Dependencies: SQLAlchemy ORM, canonical enums, database base helpers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from services.common.enums import ReviewStatus
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin
from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class Recommendation(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one accounting recommendation with its review lifecycle state.

    Recommendations are created by the LangGraph recommendation workflow (Step 27)
    and later gain journal-entry materialization, approval routing, and working-state
    application in Step 28.

    The payload column stores the structured recommendation data (account codes,
    reasoning, dimensions, risk factors) as JSONB for flexibility while the
    top-level columns provide indexed query surfaces for review queues.
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="recommendations_confidence_range",
        ),
        Index("ix_recommendations_close_run_status", "close_run_id", "status"),
        Index("ix_recommendations_document_type", "document_id", "recommendation_type"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    close_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    recommendation_type: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="Canonical recommendation type (e.g., 'gl_coding', 'journal_draft').",
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ReviewStatus.DRAFT.value,
        comment="Review lifecycle state of the recommendation.",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Structured recommendation payload (accounts, reasoning, risk).",
    )
    confidence: Mapped[float] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        comment="Aggregate confidence score between 0 and 1.",
    )
    reasoning_summary: Mapped[str] = mapped_column(
        String(5000),
        nullable=False,
        comment="Human-readable reasoning narrative for reviewer consumption.",
    )
    evidence_links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Structured references to supporting evidence sources.",
    )
    prompt_version: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Version of the prompt template used.",
    )
    rule_version: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Version of the deterministic rules used.",
    )
    schema_version: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Version of the output schema this recommendation conforms to.",
    )
    created_by_system: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        comment="Whether the recommendation was system-generated or manually created.",
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID of the recommendation that superseded this one.",
    )


__all__ = [
    "Recommendation",
]
