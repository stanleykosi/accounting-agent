"""
Purpose: Provide the canonical export and evidence-pack orchestration service.
Scope: Access checks, export triggering, evidence-pack assembly, export detail reads,
and compact export-state snapshots for operator and chat workflows.
Dependencies: SQLAlchemy ORM models, reporting/export builders, storage metadata,
and entity membership access checks through the report repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from enum import StrEnum
from uuid import UUID, uuid4

from services.audit.service import AuditService
from services.common.enums import ArtifactType
from services.contracts.export_models import (
    CreateExportRequest,
    DistributeExportRequest,
    EvidencePackBundle,
    ExportDetail,
    ExportDistributionRecord,
    ExportListResponse,
    ExportManifest,
    ExportSummary,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.documents import Document
from services.db.models.exports import Artifact, ExportDistribution, ExportRun, ExportStatus
from services.db.models.extractions import DocumentExtraction, ExtractedField
from services.db.models.reporting import ReportRun, ReportRunStatus
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.report_repo import ReportRepository
from services.idempotency.service import build_idempotency_key
from services.reporting.exports import ExportManifestInput, build_export_manifest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class ExportServiceErrorCode(StrEnum):
    """Enumerate stable export workflow failures surfaced to routes and chat."""

    ACCESS_DENIED = "close_run_access_denied"
    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    ENTITY_NOT_FOUND = "entity_not_found"
    EXPORT_NOT_FOUND = "export_not_found"
    EXPORT_NOT_READY = "export_not_ready"


class ExportServiceError(Exception):
    """Represent an expected export-domain failure with HTTP-aligned metadata."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ExportServiceErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ExportService:
    """Own the canonical export and evidence-pack workflow used by routes and chat."""

    def __init__(
        self,
        *,
        db_session: Session,
        report_repository: ReportRepository,
    ) -> None:
        self._db_session = db_session
        self._report_repo = report_repository

    def trigger_export(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        request: CreateExportRequest | None = None,
    ) -> ExportDetail:
        """Create or reuse the canonical export manifest for one close run."""

        body = request or CreateExportRequest()
        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        close_run_record, entity_record = self._require_close_run_context(
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        export_action_qualifier = _build_export_action_qualifier(
            action_qualifier=body.action_qualifier,
            include_evidence_pack=body.include_evidence_pack,
            include_audit_trail=body.include_audit_trail,
        )

        idempotency_key = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type="export_manifest",
            action_qualifier=export_action_qualifier,
            version_override=close_run_record.current_version_no,
        )
        existing_export = _load_export_run_by_idempotency(
            db_session=self._db_session,
            close_run_id=close_run_id,
            idempotency_key=idempotency_key,
        )
        if existing_export is not None:
            return _build_export_detail(
                db_session=self._db_session,
                close_run_id=close_run_id,
                export_run=existing_export,
            )

        artifact_records = _load_report_output_records(
            db_session=self._db_session,
            close_run_id=close_run_id,
        )
        evidence_pack = (
            _assemble_or_get_evidence_pack(
                db_session=self._db_session,
                entity_id=entity_id,
                close_run_id=close_run_id,
                entity_name=entity_record.name,
                close_run_version_no=close_run_record.current_version_no,
                period_start=close_run_record.period_start,
                period_end=close_run_record.period_end,
                report_output_records=artifact_records,
            )
            if body.include_evidence_pack
            else None
        )
        manifest_result = build_export_manifest(
            _build_export_manifest_input(
                close_run_id=close_run_id,
                entity_id=entity_id,
                entity_name=entity_record.name,
                period_start=datetime.combine(
                    close_run_record.period_start,
                    time.min,
                    tzinfo=UTC,
                ),
                period_end=datetime.combine(
                    close_run_record.period_end,
                    time.min,
                    tzinfo=UTC,
                ),
                close_run_version_no=close_run_record.current_version_no,
                action_qualifier=export_action_qualifier,
                artifact_records=artifact_records,
                include_evidence_pack=body.include_evidence_pack,
                include_audit_trail=body.include_audit_trail,
            )
        )
        manifest = manifest_result.manifest.model_copy(update={"evidence_pack_ref": evidence_pack})

        export_run = ExportRun(
            close_run_id=close_run_id,
            version_no=close_run_record.current_version_no,
            idempotency_key=idempotency_key,
            status=ExportStatus.COMPLETED.value,
            failure_reason=None,
            artifact_manifest=[artifact.model_dump(mode="json") for artifact in manifest.artifacts],
            evidence_pack_key=evidence_pack.idempotency_key if evidence_pack is not None else None,
            triggered_by_user_id=actor_user.id,
            completed_at=manifest.generated_at,
        )
        self._db_session.add(export_run)
        try:
            self._db_session.commit()
        except IntegrityError:
            self._db_session.rollback()
            recovered_export = _load_export_run_by_idempotency(
                db_session=self._db_session,
                close_run_id=close_run_id,
                idempotency_key=idempotency_key,
            )
            if recovered_export is None:
                raise
            return _build_export_detail(
                db_session=self._db_session,
                close_run_id=close_run_id,
                export_run=recovered_export,
            )
        self._db_session.refresh(export_run)
        return _build_export_detail(
            db_session=self._db_session,
            close_run_id=close_run_id,
            export_run=export_run,
        )

    def assemble_evidence_pack(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> EvidencePackBundle:
        """Assemble or reuse the evidence pack for one close run."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        close_run_record, entity_record = self._require_close_run_context(
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        return _assemble_or_get_evidence_pack(
            db_session=self._db_session,
            entity_id=entity_id,
            close_run_id=close_run_id,
            entity_name=entity_record.name,
            close_run_version_no=close_run_record.current_version_no,
            period_start=close_run_record.period_start,
            period_end=close_run_record.period_end,
            report_output_records=_load_report_output_records(
                db_session=self._db_session,
                close_run_id=close_run_id,
            ),
        )

    def list_exports(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> ExportListResponse:
        """List exports for one close run in newest-first order."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        return ExportListResponse(
            close_run_id=str(close_run_id),
            exports=self.list_export_summaries(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=close_run_id,
            ),
        )

    def list_export_summaries(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> tuple[ExportSummary, ...]:
        """Return export summaries for one close run."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        export_runs = (
            self._db_session.query(ExportRun)
            .filter(ExportRun.close_run_id == close_run_id)
            .order_by(ExportRun.created_at.desc())
            .all()
        )
        return tuple(
            _to_export_summary(
                export_run,
                db_session=self._db_session,
            )
            for export_run in export_runs
        )

    def read_export_detail(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        export_id: UUID,
    ) -> ExportDetail:
        """Read one export detail after verifying entity membership access."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        export_run = (
            self._db_session.query(ExportRun)
            .filter(
                ExportRun.id == export_id,
                ExportRun.close_run_id == close_run_id,
            )
            .first()
        )
        if export_run is None:
            raise ExportServiceError(
                status_code=404,
                code=ExportServiceErrorCode.EXPORT_NOT_FOUND,
                message="The requested export record does not exist for this close run.",
            )
        return _build_export_detail(
            db_session=self._db_session,
            close_run_id=close_run_id,
            export_run=export_run,
        )

    def distribute_export(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        export_id: UUID,
        request: DistributeExportRequest,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ExportDetail:
        """Record one stakeholder distribution event for a completed export package."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        export_run = (
            self._db_session.query(ExportRun)
            .filter(
                ExportRun.id == export_id,
                ExportRun.close_run_id == close_run_id,
            )
            .first()
        )
        if export_run is None:
            raise ExportServiceError(
                status_code=404,
                code=ExportServiceErrorCode.EXPORT_NOT_FOUND,
                message="The requested export record does not exist for this close run.",
            )
        if export_run.status != ExportStatus.COMPLETED.value:
            raise ExportServiceError(
                status_code=409,
                code=ExportServiceErrorCode.EXPORT_NOT_READY,
                message=(
                    "Only completed exports can be distributed. Finish export generation before "
                    "recording management delivery."
                ),
            )

        distributed_at = datetime.now(tz=UTC)
        distribution_row = ExportDistribution(
            id=uuid4(),
            export_run_id=export_run.id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            version_no=export_run.version_no,
            recipient_name=request.recipient_name,
            recipient_email=request.recipient_email,
            recipient_role=request.recipient_role,
            delivery_channel=request.delivery_channel,
            note=request.note,
            distributed_by_user_id=actor_user.id,
            distributed_at=distributed_at,
        )
        self._db_session.add(distribution_row)

        AuditService(db_session=self._db_session).emit_audit_event(
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor_user_id=actor_user.id,
            event_type="export.distributed",
            source_surface=source_surface,
            payload={
                "summary": (
                    f"{actor_user.full_name} distributed export {export_run.id} to "
                    f"{request.recipient_name} via {request.delivery_channel.replace('_', ' ')}."
                ),
                "export_id": str(export_run.id),
                "recipient_name": request.recipient_name,
                "recipient_email": request.recipient_email,
                "recipient_role": request.recipient_role,
                "delivery_channel": request.delivery_channel,
                "distributed_at": distributed_at.isoformat(),
                "note": request.note,
            },
            trace_id=trace_id,
        )
        self._db_session.commit()
        self._db_session.refresh(export_run)
        return _build_export_detail(
            db_session=self._db_session,
            close_run_id=close_run_id,
            export_run=export_run,
        )

    def get_latest_evidence_pack(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> EvidencePackBundle | None:
        """Return the latest released evidence pack reference for one close run."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        artifact = (
            self._db_session.query(Artifact)
            .filter(
                Artifact.close_run_id == close_run_id,
                Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
            )
            .order_by(Artifact.released_at.desc().nullslast(), Artifact.created_at.desc())
            .first()
        )
        if artifact is None:
            return None
        return EvidencePackBundle(
            close_run_id=str(close_run_id),
            version_no=artifact.version_no,
            generated_at=artifact.created_at,
            items=(),
            storage_key=artifact.storage_key,
            checksum=artifact.checksum,
            size_bytes=_to_int(artifact.artifact_metadata.get("size_bytes")),
            idempotency_key=artifact.idempotency_key,
        )

    def _verify_close_run_access(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> None:
        """Require entity membership access to the close run."""

        access_record = self._report_repo.get_close_run_for_entity(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=user_id,
        )
        if access_record is None:
            raise ExportServiceError(
                status_code=403,
                code=ExportServiceErrorCode.ACCESS_DENIED,
                message=(
                    "You do not have access to this close run. Verify that the entity exists, "
                    "the close run belongs to it, and you are a member of the entity workspace."
                ),
            )

    def _require_close_run_context(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
    ):
        """Load the close run and entity rows required for export assembly."""

        from services.db.models.close_run import CloseRun
        from services.db.models.entity import Entity

        close_run_record = (
            self._db_session.query(CloseRun)
            .filter(CloseRun.id == close_run_id, CloseRun.entity_id == entity_id)
            .first()
        )
        if close_run_record is None:
            raise ExportServiceError(
                status_code=404,
                code=ExportServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="The requested close run does not exist for this entity.",
            )

        entity_record = self._db_session.query(Entity).filter(Entity.id == entity_id).first()
        if entity_record is None:
            raise ExportServiceError(
                status_code=404,
                code=ExportServiceErrorCode.ENTITY_NOT_FOUND,
                message="The requested entity does not exist.",
            )

        return close_run_record, entity_record


def _build_export_action_qualifier(
    *,
    action_qualifier: str | None,
    include_evidence_pack: bool,
    include_audit_trail: bool,
) -> str:
    """Encode the caller's export options into the idempotent action qualifier."""

    base_qualifier = (action_qualifier or "full_export").strip() or "full_export"
    evidence_pack_mode = "with_evidence_pack" if include_evidence_pack else "without_evidence_pack"
    audit_trail_mode = "with_audit_trail" if include_audit_trail else "without_audit_trail"
    return f"{base_qualifier}:{evidence_pack_mode}:{audit_trail_mode}"


def _load_report_output_records(
    *,
    db_session: Session,
    close_run_id: UUID,
) -> list[dict[str, object]]:
    """Collect report artifacts from completed report runs for export assembly."""

    report_runs = (
        db_session.query(ReportRun)
        .filter(
            ReportRun.close_run_id == close_run_id,
            ReportRun.status == ReportRunStatus.COMPLETED.value,
        )
        .order_by(ReportRun.completed_at.desc().nullslast(), ReportRun.created_at.desc())
        .all()
    )
    artifact_records: list[dict[str, object]] = []
    for report_run in report_runs:
        artifact_refs = (
            report_run.artifact_refs if isinstance(report_run.artifact_refs, list) else []
        )
        for artifact_ref in artifact_refs:
            if not isinstance(artifact_ref, dict):
                continue
            artifact_type = str(artifact_ref.get("type") or "").strip()
            storage_key = str(artifact_ref.get("storage_key") or "").strip()
            if artifact_type == "" or storage_key == "":
                continue
            filename = str(artifact_ref.get("filename") or f"{artifact_type}.bin")
            artifact_records.append(
                {
                    "artifact_type": artifact_type,
                    "filename": filename,
                    "storage_key": storage_key,
                    "checksum": str(artifact_ref.get("sha256") or ""),
                    "size_bytes": _to_int(artifact_ref.get("size_bytes")),
                    "content_type": _infer_artifact_content_type(
                        artifact_type=artifact_type,
                        filename=filename,
                    ),
                    "idempotency_key": (
                        f"{close_run_id}:{artifact_type}:{report_run.id}:{filename}"
                    ),
                    "released_at": report_run.completed_at or report_run.updated_at,
                }
            )
    return artifact_records


def _assemble_or_get_evidence_pack(
    *,
    db_session: Session,
    entity_id: UUID,
    close_run_id: UUID,
    entity_name: str,
    close_run_version_no: int,
    period_start,
    period_end,
    report_output_records: list[dict[str, object]],
) -> EvidencePackBundle:
    """Return the canonical evidence-pack bundle for one close run version."""

    from services.contracts.storage_models import CloseRunStorageScope
    from services.reporting.evidence_pack import (
        EvidencePackInput,
        build_evidence_pack,
        upload_evidence_pack,
    )
    from services.storage.repository import StorageRepository

    idempotency_key = build_idempotency_key(
        close_run_id=close_run_id,
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
        action_qualifier="evidence_pack",
        version_override=close_run_version_no,
    )

    existing_artifact = _load_evidence_pack_artifact_by_idempotency(
        db_session=db_session,
        idempotency_key=idempotency_key,
    )
    if existing_artifact is not None:
        return _to_evidence_pack_bundle(
            artifact=existing_artifact,
            close_run_id=close_run_id,
        )

    source_references = _load_source_reference_records(
        db_session=db_session,
        close_run_id=close_run_id,
    )
    extracted_values = _load_extracted_value_records(
        db_session=db_session,
        close_run_id=close_run_id,
    )
    approval_records = _load_approval_records(
        db_session=db_session,
        close_run_id=close_run_id,
    )
    pack_input = EvidencePackInput(
        close_run_id=close_run_id,
        entity_id=entity_id,
        entity_name=entity_name,
        period_start=datetime.combine(period_start, time.min, tzinfo=UTC),
        period_end=datetime.combine(period_end, time.min, tzinfo=UTC),
        close_run_version_no=close_run_version_no,
        source_references=source_references,
        extracted_values=extracted_values,
        approval_records=approval_records,
        diff_entries=[],
        report_outputs=report_output_records,
    )
    pack_result = build_evidence_pack(pack_input)

    storage_repo = StorageRepository()
    scope = CloseRunStorageScope(
        entity_id=entity_id,
        close_run_id=close_run_id,
        period_start=period_start,
        period_end=period_end,
        close_run_version_no=close_run_version_no,
    )
    bundle = upload_evidence_pack(
        result=pack_result,
        storage_repo=storage_repo,
        scope=scope,
        idempotency_key=idempotency_key,
    )

    db_session.add(
        Artifact(
            close_run_id=close_run_id,
            report_run_id=None,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            storage_key=bundle.storage_key,
            mime_type="application/zip",
            checksum=bundle.checksum,
            idempotency_key=bundle.idempotency_key,
            version_no=bundle.version_no,
            released_at=bundle.generated_at,
            artifact_metadata={
                "entity_name": entity_name,
                "item_count": bundle.item_count,
                "size_bytes": bundle.size_bytes,
            },
        )
    )
    try:
        db_session.commit()
    except IntegrityError:
        db_session.rollback()
        recovered_artifact = _load_evidence_pack_artifact_by_idempotency(
            db_session=db_session,
            idempotency_key=idempotency_key,
        )
        if recovered_artifact is None:
            raise
        return _to_evidence_pack_bundle(
            artifact=recovered_artifact,
            close_run_id=close_run_id,
        )
    return bundle


def _load_source_reference_records(
    *,
    db_session: Session,
    close_run_id: UUID,
) -> list[dict[str, object]]:
    """Load source-document metadata for evidence-pack assembly."""

    document_records = (
        db_session.query(Document)
        .filter(Document.close_run_id == close_run_id)
        .order_by(Document.created_at.asc())
        .all()
    )
    return [
        {
            "document_id": str(document.id),
            "filename": document.original_filename,
            "storage_key": document.storage_key,
            "description": f"{document.document_type.replace('_', ' ')} source document",
            "sha256_checksum": document.sha256_hash,
            "size_bytes": document.file_size_bytes,
            "status": document.status,
        }
        for document in document_records
    ]


def _load_extracted_value_records(
    *,
    db_session: Session,
    close_run_id: UUID,
) -> list[dict[str, object]]:
    """Load extracted document fields for evidence-pack assembly."""

    rows = (
        db_session.query(Document, DocumentExtraction, ExtractedField)
        .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .join(ExtractedField, ExtractedField.document_extraction_id == DocumentExtraction.id)
        .filter(Document.close_run_id == close_run_id)
        .order_by(Document.created_at.asc(), ExtractedField.created_at.asc())
        .all()
    )
    extracted_values: list[dict[str, object]] = []
    for document, extraction, field in rows:
        extracted_values.append(
            {
                "document_id": str(document.id),
                "document_extraction_id": str(extraction.id),
                "field_name": field.field_name,
                "field_value": field.field_value,
                "confidence": float(field.confidence),
                "description": (
                    f"{document.original_filename} · {field.field_name.replace('_', ' ')}"
                ),
                "evidence_ref": field.evidence_ref,
            }
        )
    return extracted_values


def _load_approval_records(
    *,
    db_session: Session,
    close_run_id: UUID,
) -> list[dict[str, object]]:
    """Load recommendation and journal approvals for evidence-pack assembly."""

    from services.db.models.journals import JournalEntry
    from services.db.models.recommendations import Recommendation

    approval_records: list[dict[str, object]] = []
    approved_recommendations = (
        db_session.query(Recommendation)
        .filter(
            Recommendation.close_run_id == close_run_id,
            Recommendation.status.in_(("approved", "applied")),
        )
        .order_by(Recommendation.updated_at.asc())
        .all()
    )
    for recommendation in approved_recommendations:
        approval_records.append(
            {
                "record_type": "recommendation",
                "record_id": str(recommendation.id),
                "actor_name": "System / reviewer",
                "reason": recommendation.reasoning_summary,
                "status": recommendation.status,
            }
        )

    approved_journals = (
        db_session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run_id,
            JournalEntry.status.in_(("approved", "applied")),
        )
        .order_by(JournalEntry.updated_at.asc())
        .all()
    )
    for journal in approved_journals:
        approval_records.append(
            {
                "record_type": "journal",
                "record_id": str(journal.id),
                "actor_name": "System / reviewer",
                "reason": journal.description,
                "status": journal.status,
            }
        )
    return approval_records


def _build_export_detail(
    *,
    db_session: Session,
    close_run_id: UUID,
    export_run: ExportRun,
) -> ExportDetail:
    """Hydrate one export detail response from the persisted export run."""

    evidence_pack = _read_evidence_pack_bundle(
        db_session=db_session,
        close_run_id=close_run_id,
        idempotency_key=export_run.evidence_pack_key,
    )
    manifest = ExportManifest(
        close_run_id=str(close_run_id),
        version_no=export_run.version_no,
        generated_at=export_run.completed_at or export_run.created_at,
        artifacts=tuple(export_run.artifact_manifest or ()),
        evidence_pack_ref=evidence_pack,
    )
    summary = _to_export_summary(
        export_run,
        db_session=db_session,
        evidence_pack=evidence_pack,
    )
    return ExportDetail(
        **summary.model_dump(),
        manifest=manifest,
        evidence_pack=evidence_pack,
        distribution_records=_read_distribution_records(
            db_session=db_session,
            export_run_id=export_run.id,
        ),
    )


def _read_evidence_pack_bundle(
    *,
    db_session: Session,
    close_run_id: UUID,
    idempotency_key: str | None,
) -> EvidencePackBundle | None:
    """Resolve one evidence-pack bundle reference from the artifact table."""

    if idempotency_key is None:
        return None
    artifact = (
        db_session.query(Artifact)
        .filter(
            Artifact.close_run_id == close_run_id,
            Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
            Artifact.idempotency_key == idempotency_key,
        )
        .first()
    )
    if artifact is None:
        return None
    return _to_evidence_pack_bundle(artifact=artifact, close_run_id=close_run_id)


def _load_export_run_by_idempotency(
    *,
    db_session: Session,
    close_run_id: UUID,
    idempotency_key: str,
) -> ExportRun | None:
    """Return the export run matching one close-run/idempotency pair when present."""

    return (
        db_session.query(ExportRun)
        .filter(
            ExportRun.close_run_id == close_run_id,
            ExportRun.idempotency_key == idempotency_key,
        )
        .first()
    )


def _load_evidence_pack_artifact_by_idempotency(
    *,
    db_session: Session,
    idempotency_key: str,
) -> Artifact | None:
    """Return the released evidence-pack artifact for one idempotency key when present."""

    return (
        db_session.query(Artifact)
        .filter(
            Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
            Artifact.idempotency_key == idempotency_key,
        )
        .first()
    )


def _to_evidence_pack_bundle(
    *,
    artifact: Artifact,
    close_run_id: UUID,
) -> EvidencePackBundle:
    """Translate an evidence-pack artifact row into the bundle contract."""

    return EvidencePackBundle(
        close_run_id=str(close_run_id),
        version_no=artifact.version_no,
        generated_at=artifact.created_at,
        items=(),
        storage_key=artifact.storage_key,
        checksum=artifact.checksum,
        size_bytes=_to_int(artifact.artifact_metadata.get("size_bytes")),
        idempotency_key=artifact.idempotency_key,
    )


def _to_export_summary(
    export_run: ExportRun,
    *,
    db_session: Session,
    evidence_pack: EvidencePackBundle | None = None,
) -> ExportSummary:
    """Translate an export run row into the API summary contract."""

    artifact_count = len(export_run.artifact_manifest or [])
    if evidence_pack is not None:
        artifact_count += 1
    distribution_records = _read_distribution_records(
        db_session=db_session,
        export_run_id=export_run.id,
    )
    return ExportSummary(
        id=str(export_run.id),
        close_run_id=str(export_run.close_run_id),
        version_no=export_run.version_no,
        idempotency_key=export_run.idempotency_key,
        status=export_run.status,
        artifact_count=artifact_count,
        failure_reason=export_run.failure_reason,
        created_at=export_run.created_at,
        completed_at=export_run.completed_at,
        distribution_count=len(distribution_records),
        latest_distribution_at=(
            max(record.distributed_at for record in distribution_records)
            if distribution_records
            else None
        ),
    )


def _read_distribution_records(
    *,
    db_session: Session,
    export_run_id: UUID,
) -> tuple[ExportDistributionRecord, ...]:
    """Hydrate validated distribution records from canonical export distribution rows."""

    rows = (
        db_session.query(ExportDistribution)
        .filter(ExportDistribution.export_run_id == export_run_id)
        .order_by(ExportDistribution.distributed_at.desc(), ExportDistribution.created_at.desc())
        .all()
    )
    return tuple(
        ExportDistributionRecord(
            id=str(row.id),
            recipient_name=row.recipient_name,
            recipient_email=row.recipient_email,
            recipient_role=row.recipient_role,
            delivery_channel=row.delivery_channel,
            note=row.note,
            distributed_at=row.distributed_at,
            distributed_by_user_id=(
                str(row.distributed_by_user_id) if row.distributed_by_user_id is not None else None
            ),
        )
        for row in rows
    )


def _build_export_manifest_input(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    entity_name: str,
    period_start: datetime,
    period_end: datetime,
    close_run_version_no: int,
    action_qualifier: str | None,
    artifact_records: list[dict[str, object]],
    include_evidence_pack: bool,
    include_audit_trail: bool,
) -> ExportManifestInput:
    """Build the export-manifest input from persisted close-run outputs."""

    return ExportManifestInput(
        close_run_id=close_run_id,
        entity_id=entity_id,
        entity_name=entity_name,
        period_start=period_start,
        period_end=period_end,
        close_run_version_no=close_run_version_no,
        artifact_records=artifact_records,
        include_evidence_pack=include_evidence_pack,
        include_audit_trail=include_audit_trail,
    )


def _to_int(value: object) -> int:
    """Safely coerce a JSONB integer value to Python int."""

    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return 0


def _infer_artifact_content_type(*, artifact_type: str, filename: str) -> str:
    """Infer the content type for a report artifact reference."""

    if artifact_type == ArtifactType.GL_POSTING_PACKAGE.value or filename.endswith(".csv"):
        return "text/csv; charset=utf-8"
    if artifact_type == ArtifactType.REPORT_EXCEL.value or filename.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if artifact_type == ArtifactType.REPORT_PDF.value or filename.endswith(".pdf"):
        return "application/pdf"
    if artifact_type == ArtifactType.EVIDENCE_PACK.value or filename.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


__all__ = [
    "ExportService",
    "ExportServiceError",
    "ExportServiceErrorCode",
]
