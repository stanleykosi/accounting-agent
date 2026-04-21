"""
Purpose: Guard API and worker startup against optional-feature import and env regressions.
Scope: Import-time smoke coverage for startup modules that should not depend on
QuickBooks OAuth configuration or PDF builder availability.
Dependencies: importlib, sys.modules, and shared settings cache helpers.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from services.common.settings import AppSettings, reset_settings_cache
from services.jobs.task_names import TaskName


def _clear_modules(*prefixes: str) -> None:
    """Remove matching modules so import-time startup paths can be exercised again."""

    for module_name in list(sys.modules):
        for prefix in prefixes:
            if module_name == prefix or module_name.startswith(f"{prefix}."):
                sys.modules.pop(module_name, None)
                break


def test_api_startup_import_allows_blank_quickbooks_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure API module import does not fail when hosted QuickBooks redirect env is blank."""

    monkeypatch.setenv("quickbooks_redirect_uri", "")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)
    reset_settings_cache()
    _clear_modules("apps.api.app", "services.reporting")

    module = importlib.import_module("apps.api.app.main")

    assert module.app is not None
    assert "services.reporting.pdf_builder" not in sys.modules


def test_worker_startup_import_skips_pdf_builder_until_report_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure worker bootstrap does not eagerly import PDF generation dependencies."""

    monkeypatch.setenv("quickbooks_redirect_uri", "")
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)
    reset_settings_cache()
    _clear_modules("apps.worker.app", "services.reporting.pdf_builder")

    module = importlib.import_module("apps.worker.app.celery_app")

    assert module.celery_app is not None
    assert TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN.value in module.celery_app.tasks
    assert TaskName.CHAT_RESUME_OPERATOR_TURN.value in module.celery_app.tasks
    assert TaskName.EXPORTS_GENERATE_CLOSE_RUN_PACKAGE.value in module.celery_app.tasks
    assert TaskName.EXPORTS_ASSEMBLE_EVIDENCE_PACK.value in module.celery_app.tasks
    assert "services.reporting.pdf_builder" not in sys.modules
