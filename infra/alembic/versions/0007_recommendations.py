"""
Purpose: Add accounting recommendations table for the LangGraph recommendation workflow.
Scope: Recommendations with versioned payloads, evidence links, review lifecycle tracking,
and audit lineage. Created by Step 27 (model gateway + recommendation workflow).
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and existing close_runs/documents tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0007_recommendations"
down_revision = "0006_chart_of_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the recommendations table for accounting recommendation persistence."""

    op.create_table(
        "recommendations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "recommendation_type",
            sa.String(120),
            nullable=False,
            comment="Canonical recommendation type (e.g., 'gl_coding', 'journal_draft').",
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="draft",
            comment="Review lifecycle state of the recommendation.",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Structured recommendation payload (accounts, reasoning, risk).",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=False,
            comment="Aggregate confidence score between 0 and 1.",
        ),
        sa.Column(
            "reasoning_summary",
            sa.String(5000),
            nullable=False,
            comment="Human-readable reasoning narrative for reviewer consumption.",
        ),
        sa.Column(
            "evidence_links",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
            comment="Structured references to supporting evidence sources.",
        ),
        sa.Column(
            "prompt_version",
            sa.String(30),
            nullable=False,
            comment="Version of the prompt template used.",
        ),
        sa.Column(
            "rule_version",
            sa.String(30),
            nullable=False,
            comment="Version of the deterministic rules used.",
        ),
        sa.Column(
            "schema_version",
            sa.String(30),
            nullable=False,
            comment="Version of the output schema this recommendation conforms to.",
        ),
        sa.Column(
            "created_by_system",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
            comment="Whether the recommendation was system-generated or manually created.",
        ),
        sa.Column(
            "superseded_by_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="ID of the recommendation that superseded this one.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["close_run_id"],
            ["close_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["recommendations.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="recommendations_confidence_range",
        ),
    )

    # Indexes for review queue queries and recommendation lookups
    op.create_index(
        "ix_recommendations_close_run_status",
        "recommendations",
        ["close_run_id", "status"],
    )
    op.create_index(
        "ix_recommendations_document_type",
        "recommendations",
        ["document_id", "recommendation_type"],
    )


def downgrade() -> None:
    """Drop the recommendations table."""

    op.drop_index("ix_recommendations_document_type", table_name="recommendations")
    op.drop_index("ix_recommendations_close_run_status", table_name="recommendations")
    op.drop_table("recommendations")
