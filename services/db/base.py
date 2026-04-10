"""
Purpose: Define the canonical SQLAlchemy declarative base and shared model helpers.
Scope: Metadata naming conventions, UUID primary keys, audit timestamps, and reusable
table-check builders for the relational schema.
Dependencies: SQLAlchemy core/ORM primitives and Python standard library utilities.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Anchor all ORM models to one metadata registry with deterministic naming."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Provide an application-generated UUID primary key for canonical entities."""

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)


class TimestampedModel:
    """Attach immutable creation time and mutable update time to a model row."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def build_text_choice_check(
    *,
    column_name: str,
    values: Iterable[str],
    constraint_name: str,
) -> CheckConstraint:
    """Build a deterministic SQL `CHECK` constraint for a text column's allowed values."""

    sql_values = ", ".join(_quote_sql_literal(value) for value in values)
    return CheckConstraint(
        f"{column_name} IN ({sql_values})",
        name=constraint_name,
    )


def _quote_sql_literal(value: str) -> str:
    """Quote a trusted string literal for static check-constraint SQL."""

    escaped_value = value.replace("'", "''")
    return f"'{escaped_value}'"


__all__ = [
    "Base",
    "TimestampedModel",
    "UUIDPrimaryKeyMixin",
    "build_text_choice_check",
]
