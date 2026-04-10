"""
Purpose: Configure the canonical Alembic migration environment for the repository.
Scope: Offline SQL rendering, online migrations, and metadata registration for all ORM tables.
Dependencies: Alembic, SQLAlchemy, shared runtime settings, and services/db/models/.
"""

from __future__ import annotations

from logging.config import fileConfig

import services.db.models  # noqa: F401  # Register ORM models with Base.metadata.
from alembic import context
from services.common.settings import get_settings
from services.db.base import Base
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database.sqlalchemy_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render migration SQL without opening a live database connection."""

    context.configure(
        url=settings.database.sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=_resolve_version_table_schema(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured PostgreSQL database."""

    configuration = config.get_section(config.config_ini_section, {}) or {}
    configuration["sqlalchemy.url"] = settings.database.sqlalchemy_url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            version_table_schema=_resolve_version_table_schema(),
        )

        with context.begin_transaction():
            context.run_migrations()


def _resolve_version_table_schema() -> str | None:
    """Return a schema override only when the operator configured a non-public schema."""

    schema_name = settings.database.schema_name.strip()
    if not schema_name or schema_name == "public":
        return None

    return schema_name


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
