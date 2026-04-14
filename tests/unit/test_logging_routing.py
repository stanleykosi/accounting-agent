"""
Purpose: Verify backend logging routes informational output to stdout and true failures to stderr.
Scope: Shared logging bootstrap plus Uvicorn logger integration used by Railway deployments.
Dependencies: services/common/logging.py and services/common/settings.py.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest
from services.common.logging import configure_logging
from services.common.settings import AppSettings


def test_configure_logging_routes_uvicorn_info_to_stdout_and_errors_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure Railway-visible Uvicorn info logs do not get emitted on stderr."""

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    _reset_logging_state()
    configure_logging(AppSettings(), service_name="pytest")

    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.info("uvicorn info log")
    uvicorn_logger.error("uvicorn error log")

    _flush_all_handlers()

    assert "uvicorn info log" in stdout.getvalue()
    assert "uvicorn error log" not in stdout.getvalue()
    assert "uvicorn error log" in stderr.getvalue()
    assert "uvicorn info log" not in stderr.getvalue()


def _flush_all_handlers() -> None:
    """Flush every active logger handler so captured stream assertions stay deterministic."""

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.flush()

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            handler.flush()


def _reset_logging_state() -> None:
    """Clear global logger handlers so each test can validate a fresh logging configuration."""

    logging.shutdown()

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
