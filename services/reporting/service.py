"""
Purpose: Orchestrate report-template lifecycle, guardrail validation, report-run
management, and commentary workflows.
Scope: Template creation/activation, guardrail checks, report-run listing,
commentary creation/approval, and entity access enforcement.
Dependencies: Report repository, audit service, reporting contracts, and
shared auth/entity helpers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.common.enums import ReportSectionKey
from services.common.types import JsonObject
from services.contracts.report_models import (
    CommentarySummary,
    GuardrailValidationResponse,
    ReportRunDetail,
    ReportRunListResponse,
    ReportRunSummary,
    ReportSectionDefinition,
    ReportTemplateDetail,
    ReportTemplateListResponse,
    ReportTemplateSummary,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.reporting import CommentaryStatus, ReportTemplateSource
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.report_repo import (
    CloseRunAccessRecord,
    CommentaryRecord,
    ReportEntityRecord,
    ReportRunRecord,
    ReportTemplateRecord,
    ReportTemplateSectionRecord,
)
from services.reporting.guardrails import validate_template_guardrails

# ---------------------------------------------------------------------------
# Error domain
# ---------------------------------------------------------------------------

class ReportServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by report workflows."""

    ENTITY_NOT_FOUND = "entity_not_found"
    ENTITY_ARCHIVED = "entity_archived"
    TEMPLATE_NOT_FOUND = "template_not_found"
    TEMPLATE_NOT_ACTIVE = "template_not_active"
    TEMPLATE_GUARDRAIL_VIOLATION = "template_guardrail_violation"
    REPORT_RUN_NOT_FOUND = "report_run_not_found"
    COMMENTARY_NOT_FOUND = "commentary_not_found"
    COMMENTARY_ALREADY_APPROVED = "commentary_already_approved"


class ReportServiceError(Exception):
    """Represent an expected report-domain failure for API translation."""

    def __init__(self, *, status_code: int, code: ReportServiceErrorCode, message: str) -> None:
        """Capture HTTP status, stable error code, and recovery-oriented message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Persistence protocol
# ---------------------------------------------------------------------------

class ReportRepositoryProtocol(Protocol):
    """Describe persistence operations required by the report service."""

    def get_entity_for_user(
        self, *, entity_id: UUID, user_id: UUID
    ) -> ReportEntityRecord | None:
        """Return one entity workspace when the user has membership access."""

    def get_close_run_for_entity(
        self, *, entity_id: UUID, close_run_id: UUID, user_id: UUID
    ) -> CloseRunAccessRecord | None:
        """Return a close run when it belongs to an entity the user can access."""

    def get_active_template_for_entity(
        self, *, entity_id: UUID
    ) -> ReportTemplateRecord | None:
        """Return the active report template for an entity."""

    def get_template_by_id(
        self, *, template_id: UUID, entity_id: UUID | None = None
    ) -> ReportTemplateRecord | None:
        """Return one template by UUID, optionally scoped to an entity."""

    def list_templates_for_entity(
        self, *, entity_id: UUID
    ) -> tuple[ReportTemplateRecord, ...]:
        """Return all entity-scoped templates newest-first."""

    def list_sections_for_template(
        self, *, template_id: UUID
    ) -> tuple[ReportTemplateSectionRecord, ...]:
        """Return section definitions for one template."""

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
        """Persist one report-template row."""

    def create_template_sections(
        self, *, template_id: UUID, sections: tuple[dict[str, object], ...]
    ) -> tuple[ReportTemplateSectionRecord, ...]:
        """Persist section definition rows for one template."""

    def deactivate_all_entity_templates(self, *, entity_id: UUID) -> None:
        """Deactivate every entity-scoped template."""

    def activate_template(self, *, template_id: UUID) -> ReportTemplateRecord:
        """Activate one template and return the refreshed record."""

    def list_report_runs_for_close_run(
        self, *, close_run_id: UUID
    ) -> tuple[ReportRunRecord, ...]:
        """Return report runs for one close run."""

    def get_report_run(
        self, *, report_run_id: UUID, close_run_id: UUID | None = None
    ) -> ReportRunRecord | None:
        """Return one report run."""

    def get_latest_commentary_for_section(
        self, *, report_run_id: UUID, section_key: str
    ) -> CommentaryRecord | None:
        """Return the newest non-superseded commentary for one section."""

    def list_commentary_for_report_run(
        self, *, report_run_id: UUID
    ) -> tuple[CommentaryRecord, ...]:
        """Return all active commentary rows for one report run."""

    def create_commentary(
        self,
        *,
        report_run_id: UUID,
        section_key: str,
        status: CommentaryStatus,
        body: str,
        authored_by_user_id: UUID | None,
    ) -> CommentaryRecord:
        """Persist one commentary row."""

    def supersede_commentary(
        self, *, commentary_id: UUID, superseded_by_id: UUID
    ) -> CommentaryRecord:
        """Mark one commentary as superseded."""

    def commit(self) -> None:
        """Commit the current transaction."""

    def rollback(self) -> None:
        """Rollback the current transaction."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the exception originated from DB integrity checks."""

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> None:
        """Persist one audit event for the entity timeline."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ReportService:
    """Provide the canonical report-template and commentary workflow service."""

    def __init__(self, *, repository: ReportRepositoryProtocol) -> None:
        """Capture the persistence boundary used by report workflows."""

        self._repository = repository

    # ---- Template queries ---------------------------------------------------

    def list_templates_for_entity(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
    ) -> ReportTemplateListResponse:
        """Return all report template versions for one entity workspace."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)

        templates = self._repository.list_templates_for_entity(entity_id=entity_id)
        active_template = self._repository.get_active_template_for_entity(entity_id=entity_id)
        summaries = tuple(
            self._build_template_summary(template, entity_id=entity_id)
            for template in templates
        )

        return ReportTemplateListResponse(
            entity_id=str(entity_id),
            templates=summaries,
            active_template_id=str(active_template.id) if active_template else None,
        )

    def get_template(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        template_id: UUID,
    ) -> ReportTemplateDetail:
        """Return one report template with full section definitions."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)

        template = self._repository.get_template_by_id(
            template_id=template_id, entity_id=entity_id
        )
        if template is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.TEMPLATE_NOT_FOUND,
                message="The requested report template does not exist for this entity.",
            )

        sections = self._repository.list_sections_for_template(template_id=template.id)
        return self._build_template_detail(template, sections)

    # ---- Template mutations -------------------------------------------------

    def create_template(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        name: str,
        description: str | None,
        sections: tuple[ReportSectionDefinition, ...],
        guardrail_config: JsonObject,
        activate_immediately: bool,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ReportTemplateDetail:
        """Validate, create, and optionally activate a new entity report template."""

        self._require_mutable_entity(entity_id=entity_id, user_id=actor_user.id)

        # Validate guardrails before persisting.
        section_keys = [s.section_key for s in sections]
        is_required_map = {s.section_key: s.is_required for s in sections}
        validation = validate_template_guardrails(
            template_id="pending",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
            guardrail_config=guardrail_config if guardrail_config else None,
        )
        if not validation.is_valid:
            violation_summary = "; ".join(v.message for v in validation.violations)
            raise ReportServiceError(
                status_code=400,
                code=ReportServiceErrorCode.TEMPLATE_GUARDRAIL_VIOLATION,
                message=f"Template guardrail validation failed: {violation_summary}",
            )

        # Build canonical sections payload with computed display_order.
        section_payloads = tuple(
            self._section_to_persist(section, index)
            for index, section in enumerate(sections)
        )

        # Build sections as a plain JSONB array for the template row.
        # The database enforces jsonb_typeof(sections) = 'array'.
        sections_array: list[dict[str, object]] = [
            {
                "section_key": s.section_key,
                "label": s.label,
                "display_order": s.display_order,
                "is_required": s.is_required,
                "section_config": dict(s.section_config),
            }
            for s in sections
        ]

        try:
            # Determine version number.
            existing_templates = self._repository.list_templates_for_entity(entity_id=entity_id)
            version_no = max((t.version_no for t in existing_templates), default=0) + 1

            template = self._repository.create_template(
                entity_id=entity_id,
                source=ReportTemplateSource.ENTITY_CUSTOM,
                version_no=version_no,
                name=name,
                description=description,
                is_active=False,
                sections=sections_array,
                guardrail_config=dict(guardrail_config) if guardrail_config else {},
                created_by_user_id=actor_user.id,
            )
            self._repository.create_template_sections(
                template_id=template.id, sections=section_payloads
            )

            if activate_immediately:
                self._repository.deactivate_all_entity_templates(entity_id=entity_id)
                activated_template = self._repository.activate_template(
                    template_id=template.id
                )
            else:
                activated_template = template

            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=None,
                actor_user_id=actor_user.id,
                event_type="report.template_created",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} created report template "
                        f"'{activated_template.name}' (v{activated_template.version_no})."
                    ),
                    "template_id": serialize_uuid(activated_template.id),
                    "version_no": activated_template.version_no,
                    "activated": activate_immediately,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise ReportServiceError(
                    status_code=409,
                    code=ReportServiceErrorCode.TEMPLATE_GUARDRAIL_VIOLATION,
                    message="The new report template conflicts with existing template state.",
                ) from error
            raise

        created_sections = self._repository.list_sections_for_template(
            template_id=template.id
        )
        return self._build_template_detail(activated_template, created_sections)

    def activate_template(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        template_id: UUID,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ReportTemplateDetail:
        """Activate an existing entity report template."""

        self._require_mutable_entity(entity_id=entity_id, user_id=actor_user.id)

        template = self._repository.get_template_by_id(
            template_id=template_id, entity_id=entity_id
        )
        if template is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.TEMPLATE_NOT_FOUND,
                message="The requested report template does not exist for this entity.",
            )

        if template.is_active:
            sections = self._repository.list_sections_for_template(template_id=template.id)
            return self._build_template_detail(template, sections)

        # Validate guardrails before activation.
        sections = self._repository.list_sections_for_template(template_id=template.id)
        section_keys = [s.section_key.value for s in sections]
        is_required_map = {s.section_key.value: s.is_required for s in sections}
        validation = validate_template_guardrails(
            template_id=str(template.id),
            section_keys=section_keys,
            section_is_required_map=is_required_map,
            guardrail_config=template.guardrail_config,
        )
        if not validation.is_valid:
            violation_summary = "; ".join(v.message for v in validation.violations)
            raise ReportServiceError(
                status_code=400,
                code=ReportServiceErrorCode.TEMPLATE_GUARDRAIL_VIOLATION,
                message=f"Cannot activate template with guardrail violations: {violation_summary}",
            )

        try:
            # Activate and capture the refreshed record so the response reflects
            # the updated is_active state instead of the stale pre-activation row.
            activated_template = self._repository.activate_template(
                template_id=template.id
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=None,
                actor_user_id=actor_user.id,
                event_type="report.template_activated",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} activated report template "
                        f"'{activated_template.name}' (v{activated_template.version_no})."
                    ),
                    "template_id": serialize_uuid(activated_template.id),
                    "reason": reason,
                    "version_no": activated_template.version_no,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        refreshed_sections = self._repository.list_sections_for_template(
            template_id=template.id
        )
        return self._build_template_detail(activated_template, refreshed_sections)

    def validate_template(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        template_id: UUID,
    ) -> GuardrailValidationResponse:
        """Run guardrail validation against one template and return the result."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)

        template = self._repository.get_template_by_id(
            template_id=template_id, entity_id=entity_id
        )
        if template is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.TEMPLATE_NOT_FOUND,
                message="The requested report template does not exist for this entity.",
            )

        sections = self._repository.list_sections_for_template(template_id=template.id)
        section_keys = [s.section_key.value for s in sections]
        is_required_map = {s.section_key.value: s.is_required for s in sections}

        return validate_template_guardrails(
            template_id=str(template.id),
            section_keys=section_keys,
            section_is_required_map=is_required_map,
            guardrail_config=template.guardrail_config,
        )

    # ---- Report run queries -----------------------------------------------

    def list_report_runs(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> ReportRunListResponse:
        """Return report runs for one close run scoped to the entity."""

        self._require_close_run_access(
            entity_id=entity_id, close_run_id=close_run_id, user_id=actor_user.id
        )

        runs = self._repository.list_report_runs_for_close_run(close_run_id=close_run_id)
        return ReportRunListResponse(
            close_run_id=str(close_run_id),
            report_runs=tuple(self._build_run_summary(run) for run in runs),
        )

    def get_report_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        report_run_id: UUID,
    ) -> ReportRunDetail:
        """Return one report run with commentary details, scoped to the entity."""

        self._require_close_run_access(
            entity_id=entity_id, close_run_id=close_run_id, user_id=actor_user.id
        )

        run = self._repository.get_report_run(
            report_run_id=report_run_id, close_run_id=close_run_id
        )
        if run is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.REPORT_RUN_NOT_FOUND,
                message="The requested report run does not exist for this close run.",
            )

        commentary_records = self._repository.list_commentary_for_report_run(
            report_run_id=report_run_id
        )
        return ReportRunDetail(
            **self._build_run_summary(run).model_dump(),
            artifact_refs=list(run.artifact_refs) if isinstance(run.artifact_refs, list) else [],
            commentary=tuple(
                self._build_commentary_summary(c) for c in commentary_records
            ),
        )

    # ---- Commentary ---------------------------------------------------------

    def update_commentary(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        report_run_id: UUID,
        section_key: str,
        body: str,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CommentarySummary:
        """Update or create draft commentary for one report section, scoped to the entity."""

        self._require_close_run_access(
            entity_id=entity_id, close_run_id=close_run_id, user_id=actor_user.id
        )

        run = self._repository.get_report_run(
            report_run_id=report_run_id, close_run_id=close_run_id
        )
        if run is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.REPORT_RUN_NOT_FOUND,
                message="The requested report run does not exist for this close run.",
            )

        try:
            existing = self._repository.get_latest_commentary_for_section(
                report_run_id=report_run_id, section_key=section_key
            )

            if existing is not None and existing.status is CommentaryStatus.APPROVED:
                # Supersede the approved commentary and create a new draft.
                new_commentary = self._repository.create_commentary(
                    report_run_id=report_run_id,
                    section_key=section_key,
                    status=CommentaryStatus.DRAFT,
                    body=body,
                    authored_by_user_id=actor_user.id,
                )
                self._repository.supersede_commentary(
                    commentary_id=existing.id, superseded_by_id=new_commentary.id
                )
            elif existing is not None and existing.status in {
                CommentaryStatus.DRAFT,
                CommentaryStatus.UNDER_REVIEW,
            }:
                # Update existing non-terminal commentary in place.
                # For immutability, we supersede and create a new draft.
                new_commentary = self._repository.create_commentary(
                    report_run_id=report_run_id,
                    section_key=section_key,
                    status=existing.status,
                    body=body,
                    authored_by_user_id=actor_user.id,
                )
                self._repository.supersede_commentary(
                    commentary_id=existing.id, superseded_by_id=new_commentary.id
                )
            else:
                # No existing commentary; create fresh draft.
                new_commentary = self._repository.create_commentary(
                    report_run_id=report_run_id,
                    section_key=section_key,
                    status=CommentaryStatus.DRAFT,
                    body=body,
                    authored_by_user_id=actor_user.id,
                )

            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor_user_id=actor_user.id,
                event_type="report.commentary_updated",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} updated commentary for section "
                        f"'{section_key}' in report run {report_run_id}."
                    ),
                    "report_run_id": serialize_uuid(report_run_id),
                    "section_key": section_key,
                    "commentary_id": serialize_uuid(new_commentary.id),
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self._build_commentary_summary(new_commentary)

    def approve_commentary(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        report_run_id: UUID,
        section_key: str,
        body: str | None,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CommentarySummary:
        """Approve commentary for one report section, scoped to the entity,
        optionally with a final text edit."""

        self._require_close_run_access(
            entity_id=entity_id, close_run_id=close_run_id, user_id=actor_user.id
        )

        run = self._repository.get_report_run(
            report_run_id=report_run_id, close_run_id=close_run_id
        )
        if run is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.REPORT_RUN_NOT_FOUND,
                message="The requested report run does not exist for this close run.",
            )

        existing = self._repository.get_latest_commentary_for_section(
            report_run_id=report_run_id, section_key=section_key
        )
        if existing is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.COMMENTARY_NOT_FOUND,
                message=f"No commentary exists for section '{section_key}' in this report run.",
            )

        if existing.status is CommentaryStatus.APPROVED:
            raise ReportServiceError(
                status_code=409,
                code=ReportServiceErrorCode.COMMENTARY_ALREADY_APPROVED,
                message=f"Commentary for section '{section_key}' is already approved.",
            )

        try:
            approved_body = body if body is not None else existing.body
            approved_commentary = self._repository.create_commentary(
                report_run_id=report_run_id,
                section_key=section_key,
                status=CommentaryStatus.APPROVED,
                body=approved_body,
                authored_by_user_id=actor_user.id,
            )
            self._repository.supersede_commentary(
                commentary_id=existing.id, superseded_by_id=approved_commentary.id
            )

            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor_user_id=actor_user.id,
                event_type="report.commentary_approved",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} approved commentary for section "
                        f"'{section_key}' in report run {report_run_id}."
                    ),
                    "report_run_id": serialize_uuid(report_run_id),
                    "section_key": section_key,
                    "commentary_id": serialize_uuid(approved_commentary.id),
                    "reason": reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self._build_commentary_summary(approved_commentary)

    # ---- Internal helpers ---------------------------------------------------

    def _require_entity_access(
        self, *, entity_id: UUID, user_id: UUID
    ) -> ReportEntityRecord:
        """Fail fast when the user cannot access the entity workspace."""

        entity = self._repository.get_entity_for_user(
            entity_id=entity_id, user_id=user_id
        )
        if entity is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.ENTITY_NOT_FOUND,
                message="The requested entity workspace is not accessible.",
            )

        return entity

    def _require_mutable_entity(
        self, *, entity_id: UUID, user_id: UUID
    ) -> ReportEntityRecord:
        """Fail fast when the entity is archived or otherwise immutable."""

        entity = self._require_entity_access(entity_id=entity_id, user_id=user_id)
        if entity.status.value == "archived":
            raise ReportServiceError(
                status_code=409,
                code=ReportServiceErrorCode.ENTITY_ARCHIVED,
                message="Archived entity workspaces cannot mutate report template state.",
            )

        return entity

    def _require_close_run_access(
        self, *, entity_id: UUID, close_run_id: UUID, user_id: UUID
    ) -> CloseRunAccessRecord:
        """Fail fast when the close run does not belong to the entity the user can access."""

        access = self._repository.get_close_run_for_entity(
            entity_id=entity_id, close_run_id=close_run_id, user_id=user_id
        )
        if access is None:
            raise ReportServiceError(
                status_code=404,
                code=ReportServiceErrorCode.REPORT_RUN_NOT_FOUND,
                message="The requested close run is not accessible for this entity.",
            )

        return access

    def _build_template_summary(
        self,
        template: ReportTemplateRecord,
        *,
        entity_id: UUID,
    ) -> ReportTemplateSummary:
        """Build a lightweight template summary without section definitions."""

        sections = self._repository.list_sections_for_template(template_id=template.id)
        return self._build_template_detail(template, sections)

    def _build_template_detail(
        self,
        template: ReportTemplateRecord,
        sections: tuple[ReportTemplateSectionRecord, ...],
    ) -> ReportTemplateDetail:
        """Build a full template detail response from records."""

        section_defs = tuple(
            ReportSectionDefinition(
                section_key=s.section_key.value,
                label=s.label,
                display_order=s.display_order,
                is_required=s.is_required,
                section_config=dict(s.section_config),
            )
            for s in sections
        )

        required_keys = {
            s.section_key for s in sections if s.is_required
        }
        has_required = all(
            key in required_keys for key in ReportSectionKey
        )

        return ReportTemplateDetail(
            id=str(template.id),
            entity_id=str(template.entity_id) if template.entity_id else None,
            source=template.source.value,
            version_no=template.version_no,
            name=template.name,
            description=template.description,
            is_active=template.is_active,
            section_count=len(sections),
            has_required_sections=has_required,
            created_by_user_id=(
                str(template.created_by_user_id) if template.created_by_user_id else None
            ),
            created_at=template.created_at,
            updated_at=template.updated_at,
            sections=section_defs,
            guardrail_config=dict(template.guardrail_config),
        )

    def _build_run_summary(self, run: ReportRunRecord) -> ReportRunSummary:
        """Build a lightweight report run summary."""

        return ReportRunSummary(
            id=str(run.id),
            close_run_id=str(run.close_run_id),
            template_id=str(run.template_id),
            version_no=run.version_no,
            status=run.status.value,
            failure_reason=run.failure_reason,
            generated_by_user_id=(
                str(run.generated_by_user_id) if run.generated_by_user_id else None
            ),
            completed_at=run.completed_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def _build_commentary_summary(
        self, commentary: CommentaryRecord
    ) -> CommentarySummary:
        """Build a commentary summary for API responses."""

        return CommentarySummary(
            id=str(commentary.id),
            report_run_id=str(commentary.report_run_id),
            section_key=commentary.section_key,
            status=commentary.status.value,
            body=commentary.body,
            authored_by_user_id=(
                str(commentary.authored_by_user_id)
                if commentary.authored_by_user_id
                else None
            ),
            created_at=commentary.created_at,
            updated_at=commentary.updated_at,
        )

    @staticmethod
    def _section_to_persist(
        section: ReportSectionDefinition,
        display_order: int,
    ) -> dict[str, object]:
        """Convert a contract section definition into a persistence payload."""

        return {
            "section_key": section.section_key,
            "label": section.label,
            "display_order": display_order,
            "is_required": section.is_required,
            "section_config": dict(section.section_config),
        }


__all__ = [
    "ReportService",
    "ReportServiceError",
    "ReportServiceErrorCode",
]
