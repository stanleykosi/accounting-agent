"""
Purpose: Verify the canonical flat environment variable contract for backend settings.
Scope: Flat-name parsing for nested settings sections and fail-fast rejection
of the removed legacy names.
Dependencies: services/common/settings.py and pytest's environment monkeypatching.
"""

from __future__ import annotations

import pytest
from services.common.settings import AppSettings


def test_app_settings_reads_flat_environment_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure flat names like `api_host` and `runtime_api_base_path` populate nested settings."""

    monkeypatch.setenv("api_host", "0.0.0.0")
    monkeypatch.setenv("runtime_api_base_path", "internal-api")
    monkeypatch.setenv("database_user", "finance_user")
    monkeypatch.setenv("security_session_ttl_hours", "24")
    monkeypatch.setenv("security_credential_encryption_key", "ZmFrZS1rZXktZm9yLXNldHRpbmdzLXRlc3Q=")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.api.host == "0.0.0.0"
    assert settings.runtime.api_base_path == "/internal-api"
    assert settings.database.user == "finance_user"
    assert settings.security.credential_encryption_key is not None
    assert settings.security.session_ttl_hours == 24


def test_app_settings_ignores_removed_prefixed_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the removed long-form env variable names no longer affect runtime settings."""

    monkeypatch.setenv("ACCOUNTING_AGENT_API__HOST", "10.10.10.10")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.api.host == "127.0.0.1"


def test_app_settings_support_hosted_database_and_redis_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure hosted provider URLs can configure PostgreSQL and Redis in one step."""

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres:secret@example.supabase.co:6543/postgres?sslmode=require",
    )
    monkeypatch.setenv("REDIS_URL", "rediss://default:secret@redis.railway.internal:6379")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.api.port == 8080
    assert settings.database.connection_url.startswith("postgresql://postgres:secret@")
    assert settings.database.sqlalchemy_url.startswith("postgresql+psycopg://postgres:secret@")
    assert settings.redis.broker_url.endswith("/0")
    assert settings.redis.result_backend_url.endswith("/1")
    assert settings.redis.cache_url.endswith("/2")


def test_app_settings_support_railway_bucket_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure Railway bucket env vars map into the canonical storage settings."""

    monkeypatch.setenv("ENDPOINT", "https://storage.example.com")
    monkeypatch.setenv("ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("SECRET_ACCESS_KEY", "secret-key")
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setenv("BUCKET", "accounting-agent-assets")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.storage.endpoint == "storage.example.com"
    assert settings.storage.secure is True
    assert settings.storage.access_key == "access-key"
    assert settings.storage.secret_key is not None
    assert settings.storage.region == "us-west-2"
    assert settings.storage.document_bucket == "accounting-agent-assets"
    assert settings.storage.artifact_bucket == "accounting-agent-assets"
    assert settings.storage.derivative_bucket == "accounting-agent-assets"


def test_app_settings_accepts_quickbooks_allowed_return_origins_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the documented JSON-array env format is decoded and normalized."""

    monkeypatch.setenv(
        "quickbooks_allowed_return_origins",
        '["https://app.example.com/","https://admin.example.com"]',
    )
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.quickbooks.allowed_return_origins == (
        "https://app.example.com",
        "https://admin.example.com",
    )


def test_app_settings_accepts_quickbooks_allowed_return_origins_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the comma-delimited env format still normalizes configured origins."""

    monkeypatch.setenv(
        "quickbooks_allowed_return_origins",
        " https://app.example.com/ , https://admin.example.com ",
    )
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.quickbooks.allowed_return_origins == (
        "https://app.example.com",
        "https://admin.example.com",
    )
