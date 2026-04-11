"""
Purpose: Register the canonical ORM models with the shared SQLAlchemy metadata registry.
Scope: Import side effects only; Alembic and repositories import this module so all tables exist.
Dependencies: Individual model modules under services/db/models/.
"""

from services.db.models.audit import AuditEvent, AuditSourceSurface, ReviewAction
from services.db.models.auth import ApiToken, Session, User, UserStatus
from services.db.models.close_run import CloseRun, CloseRunPhaseState
from services.db.models.documents import Document, DocumentIssue, DocumentVersion
from services.db.models.entity import (
    DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS,
    Entity,
    EntityMembership,
    EntityStatus,
    build_default_confidence_thresholds,
)
from services.db.models.integration import (
    IntegrationConnection,
    IntegrationConnectionStatus,
    IntegrationProvider,
)
from services.db.models.ownership import OwnershipTarget

__all__ = [
    "DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS",
    "ApiToken",
    "AuditEvent",
    "AuditSourceSurface",
    "CloseRun",
    "CloseRunPhaseState",
    "Document",
    "DocumentIssue",
    "DocumentVersion",
    "Entity",
    "EntityMembership",
    "EntityStatus",
    "IntegrationConnection",
    "IntegrationConnectionStatus",
    "IntegrationProvider",
    "OwnershipTarget",
    "ReviewAction",
    "Session",
    "User",
    "UserStatus",
    "build_default_confidence_thresholds",
]
