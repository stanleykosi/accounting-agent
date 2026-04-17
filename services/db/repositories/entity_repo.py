"""
Purpose: Persist and query entity workspaces, memberships, and activity
timeline roots through SQLAlchemy.
Scope: Entity CRUD, membership management, local-user lookups, and
audit-event reads used by the entity service to build workspace
summaries and timeline views.
Dependencies: SQLAlchemy ORM sessions plus the canonical auth, entity,
and audit model definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from services.audit.service import AuditService
from services.common.enums import AutonomyMode, JobStatus
from services.common.types import JsonObject
from services.db.models.audit import AuditEvent, AuditSourceSurface, ReviewAction
from services.db.models.auth import User
from services.db.models.chat import ChatThread
from services.db.models.close_run import CloseRun, CloseRunPhaseState
from services.db.models.coa import CoaAccount, CoaMappingRule, CoaSet
from services.db.models.documents import Document, DocumentIssue, DocumentVersion
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from services.db.models.exports import Artifact, ExportDistribution, ExportRun
from services.db.models.extractions import DocumentExtraction, DocumentLineItem, ExtractedField
from services.db.models.integration import IntegrationConnection
from services.db.models.jobs import Job
from services.db.models.ownership import OwnershipTarget
from services.db.models.reporting import (
    ReportCommentary,
    ReportRun,
    ReportTemplate,
    ReportTemplateSection,
)
from sqlalchemy import delete, desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class EntityUserRecord:
    """Describe the subset of a local user row needed by entity workflows."""

    id: UUID
    email: str
    full_name: str


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """Describe one entity row used by service-layer workspace operations."""

    id: UUID
    name: str
    legal_name: str | None
    base_currency: str
    country_code: str
    timezone: str
    accounting_standard: str | None
    autonomy_mode: AutonomyMode
    default_confidence_thresholds: dict[str, float]
    status: EntityStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EntityMembershipRecord:
    """Describe one entity membership together with the member's user profile."""

    id: UUID
    entity_id: UUID
    user_id: UUID
    role: str
    is_default_actor: bool
    created_at: datetime
    updated_at: datetime
    user: EntityUserRecord


@dataclass(frozen=True, slots=True)
class EntityAccessRecord:
    """Describe one entity that the current user can access and the caller's membership row."""

    entity: EntityRecord
    membership: EntityMembershipRecord


@dataclass(frozen=True, slots=True)
class EntityActivityEventRecord:
    """Describe one entity-scoped audit event joined with its optional actor profile."""

    id: UUID
    entity_id: UUID
    event_type: str
    source_surface: AuditSourceSurface
    payload: JsonObject
    trace_id: str | None
    created_at: datetime
    actor: EntityUserRecord | None


@dataclass(frozen=True, slots=True)
class EntityDeletionPlan:
    """Describe the data and storage footprint tied to one entity workspace delete."""

    entity: EntityRecord
    close_run_count: int
    document_count: int
    thread_count: int
    active_job_ids: tuple[UUID, ...]
    source_storage_keys: tuple[str, ...]
    derivative_storage_keys: tuple[str, ...]
    artifact_storage_keys: tuple[str, ...]


class EntityRepository:
    """Execute canonical entity, membership, and timeline persistence in one DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by entity workflows."""

        self._db_session = db_session

    def list_entities_for_user(self, *, user_id: UUID) -> tuple[EntityAccessRecord, ...]:
        """Return the workspaces visible to one user together with the caller's memberships."""

        statement = (
            select(Entity, EntityMembership, User)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .join(User, User.id == EntityMembership.user_id)
            .where(EntityMembership.user_id == user_id)
            .order_by(desc(Entity.updated_at), desc(Entity.id))
        )
        rows = self._db_session.execute(statement).all()
        return tuple(
            EntityAccessRecord(
                entity=_map_entity(entity),
                membership=_map_membership(membership, user),
            )
            for entity, membership, user in rows
        )

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityAccessRecord | None:
        """Return one entity workspace and the caller's membership when access exists."""

        statement = (
            select(Entity, EntityMembership, User)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .join(User, User.id == EntityMembership.user_id)
            .where(Entity.id == entity_id, EntityMembership.user_id == user_id)
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        entity, membership, user = row
        return EntityAccessRecord(
            entity=_map_entity(entity),
            membership=_map_membership(membership, user),
        )

    def create_entity(
        self,
        *,
        name: str,
        legal_name: str | None,
        base_currency: str,
        country_code: str,
        timezone: str,
        accounting_standard: str | None,
        autonomy_mode: AutonomyMode,
    ) -> EntityRecord:
        """Stage a new entity workspace row and flush it for dependent membership writes."""

        entity = Entity(
            name=name,
            legal_name=legal_name,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            accounting_standard=accounting_standard,
            autonomy_mode=autonomy_mode.value,
        )
        self._db_session.add(entity)
        self._db_session.flush()
        return _map_entity(entity)

    def update_entity(
        self,
        *,
        entity_id: UUID,
        fields_to_update: frozenset[str],
        name: str | None = None,
        legal_name: str | None = None,
        base_currency: str | None = None,
        country_code: str | None = None,
        timezone: str | None = None,
        accounting_standard: str | None = None,
        autonomy_mode: AutonomyMode | None = None,
    ) -> EntityRecord:
        """Persist one entity workspace update and return the refreshed immutable record."""

        entity = self._load_entity(entity_id=entity_id)
        if "name" in fields_to_update and name is not None:
            entity.name = name
        if "legal_name" in fields_to_update:
            entity.legal_name = legal_name
        if "base_currency" in fields_to_update and base_currency is not None:
            entity.base_currency = base_currency
        if "country_code" in fields_to_update and country_code is not None:
            entity.country_code = country_code
        if "timezone" in fields_to_update and timezone is not None:
            entity.timezone = timezone
        if "accounting_standard" in fields_to_update:
            entity.accounting_standard = accounting_standard
        if "autonomy_mode" in fields_to_update and autonomy_mode is not None:
            entity.autonomy_mode = autonomy_mode.value

        self._db_session.flush()
        return _map_entity(entity)

    def get_user_by_email(self, *, email: str) -> EntityUserRecord | None:
        """Return one local user by canonical email when the operator already exists."""

        statement = select(User).where(User.email == email)
        user = self._db_session.execute(statement).scalar_one_or_none()
        if user is None:
            return None

        return _map_user(user)

    def list_memberships_for_entity(self, *, entity_id: UUID) -> tuple[EntityMembershipRecord, ...]:
        """Return all memberships for one entity in deterministic default-actor then name order."""

        statement = (
            select(EntityMembership, User)
            .join(User, User.id == EntityMembership.user_id)
            .where(EntityMembership.entity_id == entity_id)
            .order_by(
                desc(EntityMembership.is_default_actor),
                User.full_name.asc(),
                User.email.asc(),
            )
        )
        rows = self._db_session.execute(statement).all()
        return tuple(_map_membership(membership, user) for membership, user in rows)

    def get_membership(
        self,
        *,
        entity_id: UUID,
        membership_id: UUID,
    ) -> EntityMembershipRecord | None:
        """Return one membership by UUID when it belongs to the specified entity."""

        statement = (
            select(EntityMembership, User)
            .join(User, User.id == EntityMembership.user_id)
            .where(EntityMembership.entity_id == entity_id, EntityMembership.id == membership_id)
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        membership, user = row
        return _map_membership(membership, user)

    def get_membership_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityMembershipRecord | None:
        """Return one membership by user UUID when that user already belongs to the entity."""

        statement = (
            select(EntityMembership, User)
            .join(User, User.id == EntityMembership.user_id)
            .where(EntityMembership.entity_id == entity_id, EntityMembership.user_id == user_id)
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        membership, user = row
        return _map_membership(membership, user)

    def create_membership(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
        role: str,
        is_default_actor: bool,
    ) -> EntityMembershipRecord:
        """Stage a new entity membership row and return its immutable record view."""

        membership = EntityMembership(
            entity_id=entity_id,
            user_id=user_id,
            role=role,
            is_default_actor=is_default_actor,
        )
        self._db_session.add(membership)
        self._db_session.flush()
        user = self._load_user(user_id=user_id)
        return _map_membership(membership, user)

    def update_membership(
        self,
        *,
        membership_id: UUID,
        role: str | None = None,
        is_default_actor: bool | None = None,
    ) -> EntityMembershipRecord:
        """Persist membership changes and return the refreshed immutable record view."""

        membership = self._load_membership(membership_id=membership_id)
        if role is not None:
            membership.role = role
        if is_default_actor is not None:
            membership.is_default_actor = is_default_actor

        self._db_session.flush()
        user = self._load_user(user_id=membership.user_id)
        return _map_membership(membership, user)

    def clear_default_actor_memberships(self, *, entity_id: UUID) -> None:
        """Unset the default-actor flag for every membership in the specified entity."""

        memberships = self._db_session.execute(
            select(EntityMembership).where(
                EntityMembership.entity_id == entity_id,
                EntityMembership.is_default_actor.is_(True),
            )
        ).scalars()
        for membership in memberships:
            membership.is_default_actor = False

        self._db_session.flush()

    def count_memberships(self, *, entity_id: UUID) -> int:
        """Return the number of membership rows currently attached to one entity workspace."""

        statement = select(EntityMembership).where(EntityMembership.entity_id == entity_id)
        return len(self._db_session.execute(statement).scalars().all())

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> EntityActivityEventRecord:
        """Persist one root entity-scoped activity event used by the workspace timeline."""

        receipt = AuditService(db_session=self._db_session).emit_audit_event(
            entity_id=entity_id,
            close_run_id=None,
            event_type=event_type,
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload=dict(payload),
            trace_id=trace_id,
        )

        event = self._load_audit_event(audit_event_id=receipt.audit_event_id)
        actor = self._load_user(user_id=actor_user_id) if actor_user_id is not None else None
        return _map_activity_event(event, actor)

    def list_activity_for_entity(
        self,
        *,
        entity_id: UUID,
        limit: int,
    ) -> tuple[EntityActivityEventRecord, ...]:
        """Return recent entity-scoped activity events in newest-first order."""

        statement = (
            select(AuditEvent, User)
            .outerjoin(User, User.id == AuditEvent.actor_user_id)
            .where(AuditEvent.entity_id == entity_id)
            .order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
            .limit(limit)
        )
        rows = self._db_session.execute(statement).all()
        return tuple(
            _map_activity_event(event, user if isinstance(user, User) else None)
            for event, user in rows
        )

    def get_latest_activity_for_entities(
        self,
        *,
        entity_ids: tuple[UUID, ...],
    ) -> dict[UUID, EntityActivityEventRecord]:
        """Return the newest activity event for each requested entity ID."""

        if not entity_ids:
            return {}

        statement = (
            select(AuditEvent, User)
            .outerjoin(User, User.id == AuditEvent.actor_user_id)
            .where(AuditEvent.entity_id.in_(entity_ids))
            .order_by(AuditEvent.entity_id, desc(AuditEvent.created_at), desc(AuditEvent.id))
        )
        rows = self._db_session.execute(statement).all()
        latest_events: dict[UUID, EntityActivityEventRecord] = {}
        for event, user in rows:
            if event.entity_id in latest_events:
                continue

            latest_events[event.entity_id] = _map_activity_event(
                event,
                user if isinstance(user, User) else None,
            )

        return latest_events

    def get_entity_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityDeletionPlan | None:
        """Return the workspace footprint that will be removed by an entity delete."""

        access_record = self.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if access_record is None:
            return None

        close_run_ids = tuple(
            self._db_session.scalars(
                select(CloseRun.id).where(CloseRun.entity_id == entity_id)
            ).all()
        )
        document_rows = (
            self._db_session.execute(
                select(Document.id, Document.storage_key).where(
                    Document.close_run_id.in_(close_run_ids)
                )
            ).all()
            if close_run_ids
            else []
        )
        document_ids = tuple(document_id for document_id, _storage_key in document_rows)
        source_storage_keys = tuple(
            dict.fromkeys(
                storage_key.strip()
                for _document_id, storage_key in document_rows
                if isinstance(storage_key, str) and storage_key.strip()
            )
        )

        derivative_keys: list[str] = []
        if document_ids:
            version_rows = self._db_session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id.in_(document_ids))
                .order_by(
                    DocumentVersion.document_id.asc(),
                    DocumentVersion.version_no.asc(),
                    DocumentVersion.created_at.asc(),
                )
            ).all()
            for version in version_rows:
                if (
                    version.normalized_storage_key is not None
                    and version.normalized_storage_key.strip()
                ):
                    derivative_keys.append(version.normalized_storage_key.strip())
                if (
                    version.ocr_text_storage_key is not None
                    and version.ocr_text_storage_key.strip()
                ):
                    derivative_keys.append(version.ocr_text_storage_key.strip())
                derivative_keys.extend(
                    _collect_raw_parse_derivative_keys(raw_parse_payload=version.raw_parse_payload)
                )

        artifact_keys = self._collect_entity_artifact_storage_keys(
            entity_id=entity_id,
            close_run_ids=close_run_ids,
        )
        thread_ids = tuple(
            self._db_session.scalars(
                select(ChatThread.id).where(ChatThread.entity_id == entity_id)
            ).all()
        )
        active_job_ids = tuple(
            self._db_session.scalars(
                select(Job.id)
                .where(
                    Job.entity_id == entity_id,
                    Job.status.in_(
                        (
                            JobStatus.QUEUED.value,
                            JobStatus.RUNNING.value,
                            JobStatus.BLOCKED.value,
                        )
                    ),
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
            ).all()
        )
        return EntityDeletionPlan(
            entity=access_record.entity,
            close_run_count=len(close_run_ids),
            document_count=len(document_ids),
            thread_count=len(thread_ids),
            active_job_ids=active_job_ids,
            source_storage_keys=source_storage_keys,
            derivative_storage_keys=tuple(dict.fromkeys(derivative_keys)),
            artifact_storage_keys=artifact_keys,
        )

    def delete_entity_workspace(self, *, entity_id: UUID) -> None:
        """Delete one entity workspace and its owned graph from the database."""

        close_run_ids = tuple(
            self._db_session.scalars(
                select(CloseRun.id).where(CloseRun.entity_id == entity_id)
            ).all()
        )
        coa_set_ids = tuple(
            self._db_session.scalars(select(CoaSet.id).where(CoaSet.entity_id == entity_id)).all()
        )
        report_template_ids = tuple(
            self._db_session.scalars(
                select(ReportTemplate.id).where(ReportTemplate.entity_id == entity_id)
            ).all()
        )
        report_run_ids = (
            tuple(
                self._db_session.scalars(
                    select(ReportRun.id).where(ReportRun.close_run_id.in_(close_run_ids))
                ).all()
            )
            if close_run_ids
            else ()
        )
        document_ids = (
            tuple(
                self._db_session.scalars(
                    select(Document.id).where(Document.close_run_id.in_(close_run_ids))
                ).all()
            )
            if close_run_ids
            else ()
        )
        extraction_ids = (
            tuple(
                self._db_session.scalars(
                    select(DocumentExtraction.id).where(
                        DocumentExtraction.document_id.in_(document_ids)
                    )
                ).all()
            )
            if document_ids
            else ()
        )

        self._db_session.execute(
            update(Job)
            .where(Job.entity_id == entity_id)
            .values(entity_id=None, close_run_id=None, document_id=None)
        )
        self._db_session.execute(
            delete(OwnershipTarget).where(OwnershipTarget.entity_id == entity_id)
        )
        if close_run_ids:
            self._db_session.execute(
                delete(ReviewAction).where(ReviewAction.close_run_id.in_(close_run_ids))
            )
        self._db_session.execute(delete(AuditEvent).where(AuditEvent.entity_id == entity_id))
        self._db_session.execute(
            delete(ExportDistribution).where(ExportDistribution.entity_id == entity_id)
        )
        if close_run_ids:
            self._db_session.execute(delete(Artifact).where(Artifact.close_run_id.in_(close_run_ids)))
            if report_run_ids:
                self._db_session.execute(
                    update(ReportCommentary)
                    .where(ReportCommentary.report_run_id.in_(report_run_ids))
                    .values(superseded_by_id=None)
                )
                self._db_session.execute(
                    delete(ReportCommentary).where(ReportCommentary.report_run_id.in_(report_run_ids))
                )
            self._db_session.execute(delete(ExportRun).where(ExportRun.close_run_id.in_(close_run_ids)))
            self._db_session.execute(delete(ReportRun).where(ReportRun.close_run_id.in_(close_run_ids)))
        if report_template_ids:
            self._db_session.execute(
                delete(ReportTemplateSection).where(
                    ReportTemplateSection.template_id.in_(report_template_ids)
                )
            )
        self._db_session.execute(
            delete(ReportTemplate).where(ReportTemplate.entity_id == entity_id)
        )
        self._db_session.execute(
            delete(IntegrationConnection).where(IntegrationConnection.entity_id == entity_id)
        )
        self._db_session.execute(
            delete(CoaMappingRule).where(CoaMappingRule.entity_id == entity_id)
        )
        if coa_set_ids:
            self._db_session.execute(delete(CoaAccount).where(CoaAccount.coa_set_id.in_(coa_set_ids)))
        self._db_session.execute(delete(CoaSet).where(CoaSet.entity_id == entity_id))
        if extraction_ids:
            self._db_session.execute(
                delete(DocumentLineItem).where(DocumentLineItem.document_extraction_id.in_(extraction_ids))
            )
            self._db_session.execute(
                delete(ExtractedField).where(ExtractedField.document_extraction_id.in_(extraction_ids))
            )
            self._db_session.execute(
                delete(DocumentExtraction).where(DocumentExtraction.id.in_(extraction_ids))
            )
        if document_ids:
            self._db_session.execute(delete(DocumentIssue).where(DocumentIssue.document_id.in_(document_ids)))
            self._db_session.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
            )
            self._db_session.execute(
                update(Document).where(Document.id.in_(document_ids)).values(parent_document_id=None)
            )
            self._db_session.execute(delete(Document).where(Document.id.in_(document_ids)))
        if close_run_ids:
            self._db_session.execute(
                delete(CloseRunPhaseState).where(CloseRunPhaseState.close_run_id.in_(close_run_ids))
            )
            self._db_session.execute(
                update(CloseRun)
                .where(
                    CloseRun.entity_id == entity_id,
                    CloseRun.reopened_from_close_run_id.in_(close_run_ids),
                )
                .values(reopened_from_close_run_id=None)
            )
            self._db_session.execute(delete(CloseRun).where(CloseRun.entity_id == entity_id))
        self._db_session.execute(
            delete(EntityMembership).where(EntityMembership.entity_id == entity_id)
        )
        self._db_session.execute(delete(Entity).where(Entity.id == entity_id))
        self._db_session.flush()

    def commit(self) -> None:
        """Commit the current entity transaction after a successful mutation."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current entity transaction after a failed mutation."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the provided exception originated from a DB integrity failure."""

        return isinstance(error, IntegrityError)

    def _load_entity(self, *, entity_id: UUID) -> Entity:
        """Load one entity row by UUID or fail fast when service logic references missing data."""

        statement = select(Entity).where(Entity.id == entity_id)
        entity = self._db_session.execute(statement).scalar_one_or_none()
        if entity is None:
            raise LookupError(f"Entity {entity_id} does not exist.")

        return entity

    def _load_user(self, *, user_id: UUID) -> User:
        """Load one user row by UUID or fail fast on broken membership references."""

        statement = select(User).where(User.id == user_id)
        user = self._db_session.execute(statement).scalar_one_or_none()
        if user is None:
            raise LookupError(f"User {user_id} does not exist.")

        return user

    def _load_membership(self, *, membership_id: UUID) -> EntityMembership:
        """Load one membership row by UUID or fail fast on missing references."""

        statement = select(EntityMembership).where(EntityMembership.id == membership_id)
        membership = self._db_session.execute(statement).scalar_one_or_none()
        if membership is None:
            raise LookupError(f"Entity membership {membership_id} does not exist.")

        return membership

    def _load_audit_event(self, *, audit_event_id: UUID) -> AuditEvent:
        """Load one just-created audit event or fail fast if persistence drifted."""

        statement = select(AuditEvent).where(AuditEvent.id == audit_event_id)
        event = self._db_session.execute(statement).scalar_one_or_none()
        if event is None:
            raise LookupError(f"Audit event {audit_event_id} does not exist.")

        return event

    def _collect_entity_artifact_storage_keys(
        self,
        *,
        entity_id: UUID,
        close_run_ids: tuple[UUID, ...],
    ) -> tuple[str, ...]:
        """Return canonical artifact storage keys attached to one entity workspace."""

        artifact_keys: list[str] = [
            storage_key
            for storage_key in self._db_session.scalars(
                select(Artifact.storage_key).where(Artifact.close_run_id.in_(close_run_ids))
            ).all()
            if isinstance(storage_key, str) and storage_key.strip()
        ] if close_run_ids else []

        if close_run_ids:
            report_artifact_payloads = self._db_session.scalars(
                select(ReportRun.artifact_refs).where(ReportRun.close_run_id.in_(close_run_ids))
            ).all()
            for payload in report_artifact_payloads:
                artifact_keys.extend(_collect_storage_keys_from_json_list(payload))

            export_manifest_payloads = self._db_session.scalars(
                select(ExportRun.artifact_manifest).where(ExportRun.close_run_id.in_(close_run_ids))
            ).all()
            for payload in export_manifest_payloads:
                artifact_keys.extend(_collect_storage_keys_from_json_list(payload))

        return tuple(dict.fromkeys(artifact_keys))


def _map_user(user: User) -> EntityUserRecord:
    """Convert an ORM user row into the immutable record used by entity workflows."""

    return EntityUserRecord(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
    )


def _map_entity(entity: Entity) -> EntityRecord:
    """Convert an ORM entity row into the immutable record used by the entity service."""

    return EntityRecord(
        id=entity.id,
        name=entity.name,
        legal_name=entity.legal_name,
        base_currency=entity.base_currency,
        country_code=entity.country_code,
        timezone=entity.timezone,
        accounting_standard=entity.accounting_standard,
        autonomy_mode=_resolve_autonomy_mode(entity.autonomy_mode),
        default_confidence_thresholds=dict(entity.default_confidence_thresholds),
        status=EntityStatus(entity.status),
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


def _map_membership(membership: EntityMembership, user: User) -> EntityMembershipRecord:
    """Convert joined ORM membership and user rows into an immutable membership record."""

    return EntityMembershipRecord(
        id=membership.id,
        entity_id=membership.entity_id,
        user_id=membership.user_id,
        role=membership.role,
        is_default_actor=membership.is_default_actor,
        created_at=membership.created_at,
        updated_at=membership.updated_at,
        user=_map_user(user),
    )


def _map_activity_event(
    event: AuditEvent,
    user: User | None,
) -> EntityActivityEventRecord:
    """Convert one ORM audit event and optional actor row into an immutable timeline record."""

    return EntityActivityEventRecord(
        id=event.id,
        entity_id=event.entity_id,
        event_type=event.event_type,
        source_surface=AuditSourceSurface(event.source_surface),
        payload=cast(JsonObject, dict(event.payload)),
        trace_id=event.trace_id,
        created_at=event.created_at,
        actor=_map_user(user) if user is not None else None,
    )


def _resolve_autonomy_mode(value: str) -> AutonomyMode:
    """Resolve a stored autonomy-mode value into the canonical enum member."""

    for autonomy_mode in AutonomyMode:
        if autonomy_mode.value == value:
            return autonomy_mode

    raise ValueError(f"Unsupported autonomy mode value: {value}")


def _collect_raw_parse_derivative_keys(*, raw_parse_payload: JsonObject) -> tuple[str, ...]:
    """Return derivative storage keys persisted inside one raw parse payload."""

    raw_derivatives = raw_parse_payload.get("derivatives")
    if not isinstance(raw_derivatives, dict):
        return ()

    extracted_tables_storage_key = raw_derivatives.get("extracted_tables_storage_key")
    if not isinstance(extracted_tables_storage_key, str):
        return ()
    normalized = extracted_tables_storage_key.strip()
    if not normalized:
        return ()
    return (normalized,)


def _collect_storage_keys_from_json_list(payload: object) -> tuple[str, ...]:
    """Return storage keys from JSON arrays of artifact reference dictionaries."""

    if not isinstance(payload, list):
        return ()

    storage_keys: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        for key_name in ("storage_key", "object_key"):
            value = item.get(key_name)
            if isinstance(value, str) and value.strip():
                storage_keys.append(value.strip())

    return tuple(storage_keys)


__all__ = [
    "EntityAccessRecord",
    "EntityActivityEventRecord",
    "EntityDeletionPlan",
    "EntityMembershipRecord",
    "EntityRecord",
    "EntityRepository",
    "EntityUserRecord",
]
