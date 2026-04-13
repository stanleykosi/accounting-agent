"""
Purpose: Bootstrap canonical structured logging for the backend services.
Scope: Standard-library logging setup, structlog integration, context
binding, and audit-safe payload redaction.
Dependencies: services/common/settings.py for runtime configuration,
services/common/types.py for log format selection, and services/observability/redaction.py
for the canonical sanitization boundary.
"""

from __future__ import annotations

import logging
import logging.config
import sys
from typing import Any, cast

import structlog
from services.common.settings import AppSettings
from services.common.types import StructuredLogFormat
from services.observability.redaction import redact_log_payload
from structlog.typing import EventDict, Processor


def configure_logging(settings: AppSettings, *, service_name: str | None = None) -> None:
    """Configure standard logging and structlog for a backend service process."""

    resolved_service_name = service_name or settings.runtime.service_name
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _inject_service_name(resolved_service_name),
        timestamper,
        _redact_sensitive_fields(settings.logging.redact_fields),
    ]
    renderer = _build_renderer(settings.logging.format)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structured": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "foreign_pre_chain": shared_processors,
                    "processor": renderer,
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "level": settings.logging.level,
                    "formatter": "structured",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {
                "handlers": ["default"],
                "level": settings.logging.level,
            },
        }
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if settings.logging.include_stack_info:
        logging.getLogger(__name__).debug(
            "Structured logging configured with stack info enabled.",
            extra={"python_executable": sys.executable},
        )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the requested logger name."""

    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


def bind_log_context(**values: Any) -> None:
    """Bind contextual values that should follow logs across a request or job boundary."""

    structlog.contextvars.bind_contextvars(**values)


def clear_log_context() -> None:
    """Clear any bound log context to avoid cross-request leakage in workers."""

    structlog.contextvars.clear_contextvars()


def _build_renderer(log_format: StructuredLogFormat) -> Processor:
    """Select the renderer that best matches the configured operator output mode."""

    if log_format is StructuredLogFormat.CONSOLE:
        return structlog.dev.ConsoleRenderer()

    return structlog.processors.JSONRenderer()


def _inject_service_name(service_name: str) -> Processor:
    """Create a processor that stamps the active service name onto every event."""

    def processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service_name
        return event_dict

    return processor


def _redact_sensitive_fields(redact_fields: tuple[str, ...]) -> Processor:
    """Create a processor that redacts sensitive values from nested log payloads."""

    def processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
        return cast(
            EventDict,
            redact_log_payload(
                dict(event_dict),
                sensitive_field_names=redact_fields,
            ),
        )

    return processor
