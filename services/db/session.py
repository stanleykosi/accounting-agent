"""
Purpose: Provide the canonical SQLAlchemy engine and session-factory helpers for backend services.
Scope: Shared engine caching, per-request session creation, and
FastAPI-compatible session lifecycles.
Dependencies: SQLAlchemy session primitives and the environment-backed application settings.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from services.common.settings import AppSettings, get_settings
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=4)
def _build_engine(database_url: str, echo_sql: bool) -> Engine:
    """Create and cache one SQLAlchemy engine per canonical database configuration."""

    return create_engine(
        database_url,
        echo=echo_sql,
        pool_pre_ping=True,
    )


def get_session_factory(*, settings: AppSettings | None = None) -> sessionmaker[Session]:
    """Return the shared SQLAlchemy session factory for the active process settings."""

    resolved_settings = settings or get_settings()
    engine = _build_engine(
        resolved_settings.database.sqlalchemy_url,
        resolved_settings.database.echo_sql,
    )
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_db_session() -> Iterator[Session]:
    """Yield one database session and guarantee closure after request processing."""

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Clear the cached engine registry for controlled test reconfiguration."""

    _build_engine.cache_clear()


__all__ = ["get_db_session", "get_session_factory", "reset_engine_cache"]
