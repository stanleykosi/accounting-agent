"""
Purpose: Persist and query report templates, report runs, and commentary state.
Scope: Template CRUD, active-template resolution, versioned template creation,
report-run lifecycle management, commentary versioning, and guardrail-aware queries.
Dependencies: SQLAlchemy ORM sessions and reporting persistence models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from services.common.enums import ReportSectionKey
from services.common.types import JsonObject
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from services.db.models.reporting import (
    CommentaryStatus,
    ReportCommentary,
    ReportRun,
    ReportRunStatus,
    ReportTemplate,
    ReportTemplateSection,
    ReportTemplateSource,
)
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Immutable record types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReportTemplateRecord:
    """Describe one persisted report template as an immutable service-layer record."""

    id: UUID
    entity_id: UUID | None
    source: ReportTemplateSource
    version_no: int
    name: str
    description: str | None
    is_active: bool
    sections: list[dict[str, object]]
    guardrail_config: JsonObject
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReportTemplateSectionRecord:
    """Describe one section definition row attached to a template version."""

    id: UUID
    template_id: UUID
    section_key: ReportSectionKey
    label: str
    display_order: int
    is_required: bool
    section_config: JsonObject
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReportRunRecord:
    """Describe one report-generation run as an immutable service-layer record."""

    id: UUID
    close_run_id: UUID
    template_id: UUID
    version_no: int
    status: ReportRunStatus
    failure_reason: str | None
    generation_config: JsonObject
    artifact_refs: JsonObject
    generated_by_user_id: UUID | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CommentaryRecord:
    """Describe one commentary version as an immutable service-layer record."""

    id: UUID
    report_run_id: UUID
    section_key: str
    status: CommentaryStatus
    body: str
    authored_by_user_id: UUID | None
    superseded_by_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReportEntityRecord:
    """Describe the subset of entity fields required by report workflows."""

    id: UUID
    name: str
    status: EntityStatus


@dataclass(frozen=True, slots=True)
class CloseRunAccessRecord:
    """Describe a close run together with its accessible owning entity."""

    close_run_id: UUID
    entity_id: UUID


class ReportRepository:
    """Execute canonical reporting persistence in one request-scoped DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the SQLAlchemy session used by reporting workflows."""

        self._db_session = db_session

    # ---- Entity membership checks -------------------------------------------

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> ReportEntityRecord | None:
        """Return one entity workspace when the user has a membership that grants access."""

        statement = (
            select(Entity)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(Entity.id == entity_id, EntityMembership.user_id == user_id)
        )
        entity = self._db_session.execute(statement).scalar_one_or_none()
        return _map_entity(entity) if entity is not None else None

    def get_close_run_for_entity(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord | None:
        """Return a close run when it belongs to an entity the user can access."""

        from services.db.models.close_run import CloseRun

        statement = (
            select(CloseRun)
            .join(Entity, Entity.id == CloseRun.entity_id)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(
                CloseRun.id == close_run_id,
                CloseRun.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        close_run = self._db_session.execute(statement).scalar_one_or_none()
        if close_run is None:
            return None

        return CloseRunAccessRecord(
            close_run_id=close_run.id,
            entity_id=close_run.entity_id,
        )

    # ---- Template queries ---------------------------------------------------

    def get_active_template_for_entity(
        self,
        *,
        entity_id: UUID,
    ) -> ReportTemplateRecord | None:
        """Return the active report template for an entity, or None if no entity template exists."""

        statement = (
            select(ReportTemplate)
            .where(
                ReportTemplate.entity_id == entity_id,
                ReportTemplate.is_active.is_(True),
            )
            .limit(1)
        )
        template = self._db_session.execute(statement).scalar_one_or_none()
        return _map_template(template) if template is not None else None

    def get_active_global_template(self) -> ReportTemplateRecord | None:
        """Return the active global default template, if one exists."""

        statement = (
            select(ReportTemplate)
            .where(
                ReportTemplate.entity_id.is_(None),
                ReportTemplate.is_active.is_(True),
            )
            .order_by(desc(ReportTemplate.version_no))
            .limit(1)
        )
        template = self._db_session.execute(statement).scalar_one_or_none()
        return _map_template(template) if template is not None else None

    def get_template_by_id(
        self,
        *,
        template_id: UUID,
        entity_id: UUID | None = None,
    ) -> ReportTemplateRecord | None:
        """Return one template by UUID, optionally scoped to an entity."""

        statement = select(ReportTemplate).where(ReportTemplate.id == template_id)
        if entity_id is not None:
            statement = statement.where(ReportTemplate.entity_id == entity_id)

        template = self._db_session.execute(statement).scalar_one_or_none()
        return _map_template(template) if template is not None else None

    def list_templates_for_entity(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[ReportTemplateRecord, ...]:
        """Return all entity-scoped templates in newest-version-first order."""

        statement = (
            select(ReportTemplate)
            .where(ReportTemplate.entity_id == entity_id)
            .order_by(desc(ReportTemplate.version_no), desc(ReportTemplate.created_at))
        )
        return tuple(
            _map_template(row) for row in self._db_session.scalars(statement)
        )

    def list_sections_for_template(
        self,
        *,
        template_id: UUID,
    ) -> tuple[ReportTemplateSectionRecord, ...]:
        """Return section definitions for one template in display order."""

        statement = (
            select(ReportTemplateSection)
            .where(ReportTemplateSection.template_id == template_id)
            .order_by(ReportTemplateSection.display_order.asc())
        )
        return tuple(
            _map_section(row) for row in self._db_session.scalars(statement)
        )

    # ---- Template mutations -------------------------------------------------

    def create_template(
        self,
        *,
        entity_id: UUID | None,
        source: ReportTemplateSource,
        version_no: int,
        name: str,
        description: str | None,
        is_active: bool,
        sections: list[dict[str, object]],
        guardrail_config: JsonObject,
        created_by_user_id: UUID | None,
    ) -> ReportTemplateRecord:
        """Stage one report-template row and flush it for dependent writes."""

        template = ReportTemplate(
            entity_id=entity_id,
            source=source.value,
            version_no=version_no,
            name=name,
            description=description,
            is_active=is_active,
            sections=sections,
            guardrail_config=dict(guardrail_config),
            created_by_user_id=created_by_user_id,
        )
        self._db_session.add(template)
        self._db_session.flush()
        return _map_template(template)

    def create_template_sections(
        self,
        *,
        template_id: UUID,
        sections: tuple[dict[str, object], ...],
    ) -> tuple[ReportTemplateSectionRecord, ...]:
        """Stage section definition rows for one template and flush."""

        rows = [
            ReportTemplateSection(
                template_id=template_id,
                section_key=section["section_key"],
                label=section["label"],
                display_order=section["display_order"],
                is_required=section.get("is_required", True),
                section_config=dict(section.get("section_config", {})),
            )
            for section in sections
        ]
        self._db_session.add_all(rows)
        self._db_session.flush()
        return tuple(_map_section(row) for row in rows)

    def deactivate_all_entity_templates(
        self,
        *,
        entity_id: UUID,
    ) -> None:
        """Deactivate every active report template for one entity."""

        statement = (
            update(ReportTemplate)
            .where(ReportTemplate.entity_id == entity_id, ReportTemplate.is_active.is_(True))
            .values(is_active=False)
        )
        self._db_session.execute(statement)
        self._db_session.flush()

    def deactivate_global_template(self) -> None:
        """Deactivate the active global default template."""

        statement = (
            update(ReportTemplate)
            .where(
                ReportTemplate.entity_id.is_(None),
                ReportTemplate.is_active.is_(True),
            )
            .values(is_active=False)
        )
        self._db_session.execute(statement)
        self._db_session.flush()

    def activate_template(
        self,
        *,
        template_id: UUID,
    ) -> ReportTemplateRecord:
        """Activate one report template and return the refreshed record."""

        template = self._load_template(template_id=template_id)
        template.is_active = True
        self._db_session.flush()
        return _map_template(template)

    # ---- Report run queries -------------------------------------------------

    def get_report_run(
        self,
        *,
        report_run_id: UUID,
        close_run_id: UUID | None = None,
    ) -> ReportRunRecord | None:
        """Return one report run by UUID, optionally scoped to a close run."""

        statement = select(ReportRun).where(ReportRun.id == report_run_id)
        if close_run_id is not None:
            statement = statement.where(ReportRun.close_run_id == close_run_id)

        run = self._db_session.execute(statement).scalar_one_or_none()
        return _map_report_run(run) if run is not None else None

    def list_report_runs_for_close_run(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[ReportRunRecord, ...]:
        """Return report runs for one close run in newest-first order."""

        statement = (
            select(ReportRun)
            .where(ReportRun.close_run_id == close_run_id)
            .order_by(desc(ReportRun.version_no), desc(ReportRun.created_at))
        )
        return tuple(_map_report_run(row) for row in self._db_session.scalars(statement))

    def next_version_no_for_close_run(
        self,
        *,
        close_run_id: UUID,
    ) -> int:
        """Return the next report-run version number for a close run."""

        statement = select(func.max(ReportRun.version_no)).where(
            ReportRun.close_run_id == close_run_id
        )
        current_max = self._db_session.execute(statement).scalar_one_or_none()
        return int(current_max or 0) + 1

    # ---- Report run mutations -----------------------------------------------

    def create_report_run(
        self,
        *,
        close_run_id: UUID,
        template_id: UUID,
        version_no: int,
        status: ReportRunStatus,
        generation_config: JsonObject,
        generated_by_user_id: UUID | None,
    ) -> ReportRunRecord:
        """Stage one report-run row and flush it for dependent writes."""

        run = ReportRun(
            close_run_id=close_run_id,
            template_id=template_id,
            version_no=version_no,
            status=status.value,
            generation_config=dict(generation_config),
            generated_by_user_id=generated_by_user_id,
        )
        self._db_session.add(run)
        self._db_session.flush()
        return _map_report_run(run)

    def update_report_run_status(
        self,
        *,
        report_run_id: UUID,
        status: ReportRunStatus,
        failure_reason: str | None = None,
        artifact_refs: JsonObject | None = None,
        completed_at: datetime | None = None,
    ) -> ReportRunRecord:
        """Update the status of one report run and return the refreshed record."""

        run = self._load_report_run(report_run_id=report_run_id)
        run.status = status.value
        if failure_reason is not None:
            run.failure_reason = failure_reason
        if artifact_refs is not None:
            run.artifact_refs = dict(artifact_refs)
        if completed_at is not None:
            run.completed_at = completed_at

        self._db_session.flush()
        return _map_report_run(run)

    # ---- Commentary queries -------------------------------------------------

    def get_latest_commentary_for_section(
        self,
        *,
        report_run_id: UUID,
        section_key: str,
    ) -> CommentaryRecord | None:
        """Return the newest non-superseded commentary for one section in a report run."""

        statement = (
            select(ReportCommentary)
            .where(
                ReportCommentary.report_run_id == report_run_id,
                ReportCommentary.section_key == section_key,
                ReportCommentary.status.in_(
                    (
                        CommentaryStatus.DRAFT.value,
                        CommentaryStatus.UNDER_REVIEW.value,
                        CommentaryStatus.APPROVED.value,
                    )
                ),
            )
            .order_by(desc(ReportCommentary.created_at))
            .limit(1)
        )
        commentary = self._db_session.execute(statement).scalar_one_or_none()
        return _map_commentary(commentary) if commentary is not None else None

    def list_commentary_for_report_run(
        self,
        *,
        report_run_id: UUID,
    ) -> tuple[CommentaryRecord, ...]:
        """Return all active commentary rows for one report run."""

        statement = (
            select(ReportCommentary)
            .where(
                ReportCommentary.report_run_id == report_run_id,
                ReportCommentary.status.in_(
                    (
                        CommentaryStatus.DRAFT.value,
                        CommentaryStatus.UNDER_REVIEW.value,
                        CommentaryStatus.APPROVED.value,
                    )
                ),
            )
            .order_by(ReportCommentary.section_key.asc())
        )
        return tuple(
            _map_commentary(row) for row in self._db_session.scalars(statement)
        )

    # ---- Commentary mutations -----------------------------------------------

    def create_commentary(
        self,
        *,
        report_run_id: UUID,
        section_key: str,
        status: CommentaryStatus,
        body: str,
        authored_by_user_id: UUID | None,
    ) -> CommentaryRecord:
        """Stage one commentary row and flush it for dependent writes."""

        commentary = ReportCommentary(
            report_run_id=report_run_id,
            section_key=section_key,
            status=status.value,
            body=body,
            authored_by_user_id=authored_by_user_id,
        )
        self._db_session.add(commentary)
        self._db_session.flush()
        return _map_commentary(commentary)

    def supersede_commentary(
        self,
        *,
        commentary_id: UUID,
        superseded_by_id: UUID,
    ) -> CommentaryRecord:
        """Mark one commentary row as superseded and return the refreshed record."""

        commentary = self._load_commentary(commentary_id=commentary_id)
        commentary.status = CommentaryStatus.SUPERSEDED.value
        commentary.superseded_by_id = superseded_by_id
        self._db_session.flush()
        return _map_commentary(commentary)

    # ---- Transaction control ------------------------------------------------

    def commit(self) -> None:
        """Commit the current reporting transaction after a successful mutation."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current reporting transaction after a failed mutation."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the provided exception originated from a DB integrity failure."""

        return isinstance(error, IntegrityError)

    # ---- Internal helpers ---------------------------------------------------

    def _load_template(self, *, template_id: UUID) -> ReportTemplate:
        """Load one report template by UUID or fail fast."""

        statement = select(ReportTemplate).where(ReportTemplate.id == template_id)
        template = self._db_session.execute(statement).scalar_one_or_none()
        if template is None:
            raise LookupError(f"Report template {template_id} does not exist.")

        return template

    def _load_report_run(self, *, report_run_id: UUID) -> ReportRun:
        """Load one report run by UUID or fail fast."""

        statement = select(ReportRun).where(ReportRun.id == report_run_id)
        run = self._db_session.execute(statement).scalar_one_or_none()
        if run is None:
            raise LookupError(f"Report run {report_run_id} does not exist.")

        return run

    def _load_commentary(self, *, commentary_id: UUID) -> ReportCommentary:
        """Load one commentary row by UUID or fail fast."""

        statement = select(ReportCommentary).where(ReportCommentary.id == commentary_id)
        commentary = self._db_session.execute(statement).scalar_one_or_none()
        if commentary is None:
            raise LookupError(f"Report commentary {commentary_id} does not exist.")

        return commentary


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

def _map_template(template: ReportTemplate) -> ReportTemplateRecord:
    """Convert an ORM template row into the immutable repository record."""

    raw_sections = template.sections
    if isinstance(raw_sections, dict):
        # Legacy format: {"sections": [...]}. Extract the array.
        sections_list = raw_sections.get("sections", [])
    elif isinstance(raw_sections, list):
        sections_list = raw_sections
    else:
        sections_list = []

    return ReportTemplateRecord(
        id=template.id,
        entity_id=template.entity_id,
        source=_resolve_template_source(template.source),
        version_no=template.version_no,
        name=template.name,
        description=template.description,
        is_active=template.is_active,
        sections=list(sections_list),
        guardrail_config=dict(template.guardrail_config),
        created_by_user_id=template.created_by_user_id,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _map_section(section: ReportTemplateSection) -> ReportTemplateSectionRecord:
    """Convert an ORM section row into the immutable repository record."""

    return ReportTemplateSectionRecord(
        id=section.id,
        template_id=section.template_id,
        section_key=_resolve_section_key(section.section_key),
        label=section.label,
        display_order=section.display_order,
        is_required=section.is_required,
        section_config=dict(section.section_config),
        created_at=section.created_at,
        updated_at=section.updated_at,
    )


def _map_report_run(run: ReportRun) -> ReportRunRecord:
    """Convert an ORM report run row into the immutable repository record."""

    return ReportRunRecord(
        id=run.id,
        close_run_id=run.close_run_id,
        template_id=run.template_id,
        version_no=run.version_no,
        status=_resolve_report_run_status(run.status),
        failure_reason=run.failure_reason,
        generation_config=dict(run.generation_config),
        artifact_refs=dict(run.artifact_refs),
        generated_by_user_id=run.generated_by_user_id,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _map_commentary(commentary: ReportCommentary) -> CommentaryRecord:
    """Convert an ORM commentary row into the immutable repository record."""

    return CommentaryRecord(
        id=commentary.id,
        report_run_id=commentary.report_run_id,
        section_key=commentary.section_key,
        status=_resolve_commentary_status(commentary.status),
        body=commentary.body,
        authored_by_user_id=commentary.authored_by_user_id,
        superseded_by_id=commentary.superseded_by_id,
        created_at=commentary.created_at,
        updated_at=commentary.updated_at,
    )


def _resolve_template_source(value: str) -> ReportTemplateSource:
    """Resolve a stored template source value or fail fast on schema drift."""

    for source in ReportTemplateSource:
        if source.value == value:
            return source

    raise ValueError(f"Unsupported report template source value: {value}")


def _resolve_section_key(value: str) -> ReportSectionKey:
    """Resolve a stored section key value or fail fast on schema drift."""

    for key in ReportSectionKey:
        if key.value == value:
            return key

    raise ValueError(f"Unsupported report section key value: {value}")


def _resolve_report_run_status(value: str) -> ReportRunStatus:
    """Resolve a stored report run status value or fail fast on schema drift."""

    for status in ReportRunStatus:
        if status.value == value:
            return status

    raise ValueError(f"Unsupported report run status value: {value}")


def _resolve_commentary_status(value: str) -> CommentaryStatus:
    """Resolve a stored commentary status value or fail fast on schema drift."""

    for status in CommentaryStatus:
        if status.value == value:
            return status

    raise ValueError(f"Unsupported commentary status value: {value}")


def _map_entity(entity: Entity) -> ReportEntityRecord:
    """Convert an ORM entity row into the immutable report entity record."""

    return ReportEntityRecord(
        id=entity.id,
        name=entity.name,
        status=EntityStatus(entity.status),
    )


__all__ = [
    "CloseRunAccessRecord",
    "CommentaryRecord",
    "ReportEntityRecord",
    "ReportRepository",
    "ReportRunRecord",
    "ReportTemplateRecord",
    "ReportTemplateSectionRecord",
]
