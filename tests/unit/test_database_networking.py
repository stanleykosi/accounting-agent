"""
Purpose: Verify hosted PostgreSQL connections prefer reachable IPv4 routes when DNS also exposes IPv6.
Scope: Database settings hostaddr resolution, startup healthcheck wiring, and SQLAlchemy engine wiring.
Dependencies: Shared settings, runtime checks, and DB session helpers.
"""

from __future__ import annotations

from typing import Any

import services.common.settings as settings_module
import services.common.runtime_checks as runtime_checks_module
import services.db.session as session_module
from services.common.settings import AppSettings


def test_database_settings_resolve_preferred_hostaddr_for_hosted_url(
    monkeypatch,
) -> None:
    """Ensure hosted database URLs resolve an IPv4 hostaddr when DNS exposes one."""

    settings_module._resolve_ipv4_hostaddr.cache_clear()
    monkeypatch.setattr(
        settings_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (0, 0, 0, "", ("203.0.113.10", 0)),
        ],
    )
    settings = AppSettings.model_validate(
        {
            "database": {
                "url": "postgresql://postgres:secret@db-host-1.internal:5432/postgres?sslmode=require"
            }
        }
    )

    assert settings.database.resolve_preferred_hostaddr() == "203.0.113.10"


def test_verify_database_connectivity_uses_resolved_hostaddr(monkeypatch) -> None:
    """Ensure startup connectivity checks pass the resolved hostaddr into psycopg."""

    settings_module._resolve_ipv4_hostaddr.cache_clear()
    monkeypatch.setattr(
        settings_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (0, 0, 0, "", ("198.51.100.25", 0)),
        ],
    )
    settings = AppSettings.model_validate(
        {
            "database": {
                "url": "postgresql://postgres:secret@db-host-2.internal:5432/postgres?sslmode=require"
            }
        }
    )
    connect_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeCursor:
        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, query: str) -> None:
            assert query == "SELECT 1;"

        def fetchone(self) -> tuple[int]:
            return (1,)

    class _FakeConnection:
        def __enter__(self) -> _FakeConnection:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    def fake_connect(conninfo: str, **kwargs: Any) -> _FakeConnection:
        connect_calls.append((conninfo, kwargs))
        return _FakeConnection()

    monkeypatch.setattr(runtime_checks_module.psycopg, "connect", fake_connect)

    runtime_checks_module.verify_database_connectivity(settings)

    assert connect_calls == [
        (
            settings.database.connection_url,
            {"connect_timeout": 5, "hostaddr": "198.51.100.25"},
        )
    ]


def test_session_factory_builds_engine_with_resolved_hostaddr(monkeypatch) -> None:
    """Ensure ORM sessions use the same IPv4 hostaddr override as startup checks."""

    settings_module._resolve_ipv4_hostaddr.cache_clear()
    session_module.reset_engine_cache()
    monkeypatch.setattr(
        settings_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (0, 0, 0, "", ("192.0.2.44", 0)),
        ],
    )
    settings = AppSettings.model_validate(
        {
            "database": {
                "url": "postgresql://postgres:secret@db-host-3.internal:5432/postgres?sslmode=require"
            }
        }
    )
    create_engine_calls: list[dict[str, Any]] = []

    def fake_create_engine(database_url: str, **kwargs: Any) -> str:
        create_engine_calls.append({"database_url": database_url, **kwargs})
        return "engine"

    def fake_sessionmaker(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    monkeypatch.setattr(session_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(session_module, "sessionmaker", fake_sessionmaker)

    session_factory = session_module.get_session_factory(settings=settings)

    assert session_factory["bind"] == "engine"
    assert create_engine_calls == [
        {
            "database_url": settings.database.sqlalchemy_url,
            "connect_args": {"hostaddr": "192.0.2.44"},
            "echo": False,
            "pool_pre_ping": True,
        }
    ]
