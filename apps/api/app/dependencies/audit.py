"""
Purpose: Expose the canonical FastAPI audit-service dependency.
Scope: Request-scoped audit emitter construction and request trace-id extraction for routes.
Dependencies: FastAPI dependency injection, the shared DB dependency, and services/audit/service.py.
"""

from __future__ import annotations

from typing import Annotated

from apps.api.app.dependencies.db import DatabaseSessionDependency
from fastapi import Depends, Request
from services.audit.service import AuditService


def get_audit_service(db_session: DatabaseSessionDependency) -> AuditService:
    """Construct the canonical audit emitter from the request-scoped DB session."""

    return AuditService(db_session=db_session)


def resolve_request_trace_id(request: Request) -> str | None:
    """Return the request identifier bound by middleware for persisted audit correlation."""

    request_id = getattr(request.state, "request_id", None)
    return str(request_id) if request_id is not None else None


AuditServiceDependency = Annotated[AuditService, Depends(get_audit_service)]

__all__ = ["AuditServiceDependency", "get_audit_service", "resolve_request_trace_id"]

