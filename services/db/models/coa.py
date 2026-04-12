"""
Purpose: Define canonical chart-of-accounts persistence models and mapping-rule tables.
Scope: Versioned COA sets, account rows, and reusable mapping rules that back
manual uploads, QuickBooks sync imports, and the Nigerian SME fallback template.
Dependencies: SQLAlchemy ORM primitives, PostgreSQL JSONB support, and shared
DB base helpers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from services.common.types import JsonObject
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class CoaSetSource(StrEnum):
    """Enumerate canonical chart-of-accounts sources persisted per entity."""

    MANUAL_UPLOAD = "manual_upload"
    QUICKBOOKS_SYNC = "quickbooks_sync"
    FALLBACK_NIGERIAN_SME = "fallback_nigerian_sme"


class CoaSet(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one immutable chart-of-accounts set version for an entity workspace."""

    __tablename__ = "coa_sets"
    __table_args__ = (
        build_text_choice_check(
            column_name="source",
            values=tuple(source.value for source in CoaSetSource),
            constraint_name="source_valid",
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        UniqueConstraint("entity_id", "version_no", name="uq_coa_sets_entity_version"),
        # One active COA set is canonical for an entity at any point in time.
        Index(
            "uq_coa_sets_entity_active",
            "entity_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_coa_sets_entity_id_source", "entity_id", "source"),
        Index("ix_coa_sets_entity_id_version_no", "entity_id", "version_no"),
    )

    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    import_metadata: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CoaAccount(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one account row attached to a specific immutable COA set version."""

    __tablename__ = "coa_accounts"
    __table_args__ = (
        UniqueConstraint("coa_set_id", "account_code", name="uq_coa_accounts_set_code"),
        Index("ix_coa_accounts_coa_set_id_account_type", "coa_set_id", "account_type"),
        Index("ix_coa_accounts_coa_set_id_account_code", "coa_set_id", "account_code"),
    )

    coa_set_id: Mapped[UUID] = mapped_column(ForeignKey("coa_sets.id"), nullable=False)
    account_code: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    account_type: Mapped[str] = mapped_column(String, nullable=False)
    parent_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("coa_accounts.id"),
        nullable=True,
    )
    is_postable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    dimension_defaults: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class CoaMappingRule(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one reusable mapping rule that resolves extracted transactions to COA targets."""

    __tablename__ = "coa_mapping_rules"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        Index("ix_coa_mapping_rules_entity_priority", "entity_id", "priority"),
        Index("ix_coa_mapping_rules_entity_active", "entity_id", "is_active"),
    )

    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        server_default=text("100"),
    )
    match_conditions: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    target_account_id: Mapped[UUID] = mapped_column(ForeignKey("coa_accounts.id"), nullable=False)
    target_dimensions: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_from_override: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


__all__ = ["CoaAccount", "CoaMappingRule", "CoaSet", "CoaSetSource"]
