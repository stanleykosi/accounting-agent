"""
Purpose: Register the canonical ORM models with the shared SQLAlchemy metadata registry.
Scope: Import side effects only; Alembic and repositories import this module so all tables exist.
Dependencies: Individual model modules under services/db/models/.
"""

from services.db.models.audit import AuditEvent, AuditSourceSurface, ReviewAction
from services.db.models.auth import ApiToken, Session, User, UserStatus
from services.db.models.chat import ChatMessage, ChatThread
from services.db.models.chat_action_plans import ChatActionPlan
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
from services.db.models.exports import Artifact, ExportDistribution, ExportRun, ExportStatus
from services.db.models.extractions import DocumentExtraction, DocumentLineItem, ExtractedField
from services.db.models.integration import (
    IntegrationConnection,
    IntegrationConnectionStatus,
    IntegrationProvider,
)
from services.db.models.jobs import Job
from services.db.models.journals import (
    JournalEntry,
    JournalLine,
    JournalPosting,
    JournalPostingStatus,
    JournalPostingTarget,
)
from services.db.models.ownership import OwnershipTarget
from services.db.models.recommendations import Recommendation
from services.db.models.reconciliation import (
    Reconciliation,
    ReconciliationAnomaly,
    ReconciliationItem,
    TrialBalanceSnapshot,
)
from services.db.models.reporting import (
    CommentaryStatus,
    ReportCommentary,
    ReportRun,
    ReportRunStatus,
    ReportTemplate,
    ReportTemplateSection,
    ReportTemplateSource,
)
from services.db.models.supporting_schedules import SupportingSchedule, SupportingScheduleRow

__all__ = [
    "DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS",
    "ApiToken",
    "Artifact",
    "AuditEvent",
    "AuditSourceSurface",
    "ChatActionPlan",
    "ChatMessage",
    "ChatThread",
    "CloseRun",
    "CloseRunPhaseState",
    "CoaAccount",
    "CoaMappingRule",
    "CoaSet",
    "CoaSetSource",
    "CommentaryStatus",
    "Document",
    "DocumentExtraction",
    "DocumentIssue",
    "DocumentLineItem",
    "DocumentVersion",
    "Entity",
    "EntityMembership",
    "EntityStatus",
    "ExportDistribution",
    "ExportRun",
    "ExportStatus",
    "ExtractedField",
    "IntegrationConnection",
    "IntegrationConnectionStatus",
    "IntegrationProvider",
    "Job",
    "JournalEntry",
    "JournalLine",
    "JournalPosting",
    "JournalPostingStatus",
    "JournalPostingTarget",
    "OwnershipTarget",
    "Recommendation",
    "Reconciliation",
    "ReconciliationAnomaly",
    "ReconciliationItem",
    "ReportCommentary",
    "ReportRun",
    "ReportRunStatus",
    "ReportTemplate",
    "ReportTemplateSection",
    "ReportTemplateSource",
    "ReviewAction",
    "Session",
    "SupportingSchedule",
    "SupportingScheduleRow",
    "TrialBalanceSnapshot",
    "User",
    "UserStatus",
    "build_default_confidence_thresholds",
]
