"""
Purpose: Register the canonical ORM models with the shared SQLAlchemy metadata registry.
Scope: Import side effects only; Alembic and repositories import this module so all tables exist.
Dependencies: Individual model modules under services/db/models/.
"""

from services.db.models.audit import AuditEvent, AuditSourceSurface, ReviewAction
from services.db.models.auth import ApiToken, Session, User, UserStatus
from services.db.models.close_run import CloseRun, CloseRunPhaseState
from services.db.models.coa import CoaAccount, CoaMappingRule, CoaSet, CoaSetSource
from services.db.models.documents import Document, DocumentIssue, DocumentVersion
from services.db.models.entity import (
    DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS,
    Entity,
    EntityMembership,
    EntityStatus,
    build_default_confidence_thresholds,
)
from services.db.models.extractions import DocumentExtraction, DocumentLineItem, ExtractedField
from services.db.models.integration import (
    IntegrationConnection,
    IntegrationConnectionStatus,
    IntegrationProvider,
)
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ownership import OwnershipTarget
from services.db.models.recommendations import Recommendation

__all__ = [
    "DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS",
    "ApiToken",
    "AuditEvent",
    "AuditSourceSurface",
    "CloseRun",
    "CloseRunPhaseState",
    "CoaAccount",
    "CoaMappingRule",
    "CoaSet",
    "CoaSetSource",
    "Document",
    "DocumentExtraction",
    "DocumentIssue",
    "DocumentLineItem",
    "DocumentVersion",
    "Entity",
    "EntityMembership",
    "EntityStatus",
    "ExtractedField",
    "IntegrationConnection",
    "IntegrationConnectionStatus",
    "IntegrationProvider",
    "JournalEntry",
    "JournalLine",
    "OwnershipTarget",
    "Recommendation",
    "ReviewAction",
    "Session",
    "User",
    "UserStatus",
    "build_default_confidence_thresholds",
]
