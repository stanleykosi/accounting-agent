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
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.api.host == "0.0.0.0"
    assert settings.runtime.api_base_path == "/internal-api"
    assert settings.database.user == "finance_user"
    assert settings.security.session_ttl_hours == 24


def test_app_settings_ignores_removed_prefixed_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the removed long-form env variable names no longer affect runtime settings."""

    monkeypatch.setenv("ACCOUNTING_AGENT_API__HOST", "10.10.10.10")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)

    settings = AppSettings()

    assert settings.api.host == "127.0.0.1"
