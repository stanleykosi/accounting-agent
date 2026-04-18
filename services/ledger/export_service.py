"""
Purpose: Generate and read close-run general-ledger export artifacts from the canonical
effective-ledger state.
Scope: Access checks, effective-ledger CSV assembly, idempotent artifact release, and
download metadata reads for current close-run versions.
Dependencies: SQLAlchemy ORM models, shared effective-ledger loaders, storage repository,
and canonical idempotency helpers.
"""

from __future__ import annotations

import csv
import json
from enum import StrEnum
from io import StringIO
from uuid import UUID

from services.common.enums import ArtifactType
from services.common.types import JsonObject, utc_now
from services.contracts.ledger_models import GeneralLedgerExportSummary
from services.contracts.storage_models import CloseRunStorageScope
from services.db.models.close_run import CloseRun
from services.db.models.entity import Entity, EntityMembership
from services.db.models.exports import Artifact
from services.idempotency.service import build_idempotency_key
from services.ledger.effective_ledger import (
    load_close_run_ledger_binding,
    load_effective_ledger_transactions,
)
from services.storage.checksums import compute_sha256_bytes
from services.storage.repository import StorageRepository
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class GeneralLedgerExportServiceErrorCode(StrEnum):
    """Enumerate stable errors surfaced by the close-run GL export workflow."""

    ACCESS_DENIED = "access_denied"
    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    ENTITY_NOT_FOUND = "entity_not_found"
    INTEGRITY_CONFLICT = "integrity_conflict"
    NO_LEDGER_DATA = "no_ledger_data"


class GeneralLedgerExportServiceError(Exception):
    """Represent one expected close-run GL export failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: GeneralLedgerExportServiceErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class GeneralLedgerExportService:
    """Generate and read effective-ledger exports for one close run."""

    def __init__(
        self,
        *,
        db_session: Session,
        storage_repository: StorageRepository | None = None,
    ) -> None:
        self._db_session = db_session
        self._storage_repository = storage_repository or StorageRepository()

    def generate_export(
        self,
        *,
        actor_user,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> GeneralLedgerExportSummary:
        """Generate or reuse the current-version GL export for one close run."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        close_run_record, _entity_record = self._require_close_run_context(
            entity_id=entity_id,
            close_run_id=close_run_id,
        )

        transactions = load_effective_ledger_transactions(self._db_session, close_run_id)
        if not transactions:
            raise GeneralLedgerExportServiceError(
                status_code=409,
                code=GeneralLedgerExportServiceErrorCode.NO_LEDGER_DATA,
                message=(
                    "No transaction-level ledger data is available for export. Bind an imported "
                    "general ledger baseline or approve/apply close-run journals first."
                ),
            )

        payload, export_metadata = _build_general_ledger_csv_payload(
            close_run=close_run_record,
            close_run_id=close_run_id,
            transactions=transactions,
            binding=load_close_run_ledger_binding(self._db_session, close_run_id),
        )
        payload_checksum = compute_sha256_bytes(payload)
        idempotency_key = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type=ArtifactType.GENERAL_LEDGER_EXPORT.value,
            action_qualifier="effective_ledger",
            version_override=close_run_record.current_version_no,
            extra_segments=(payload_checksum[:16],),
        )
        existing_artifact = _load_export_artifact_by_idempotency(
            db_session=self._db_session,
            close_run_id=close_run_id,
            idempotency_key=idempotency_key,
        )
        if existing_artifact is not None:
            return _to_general_ledger_export_summary(
                artifact=existing_artifact,
                close_run=close_run_record,
            )

        filename = (
            f"effective-general-ledger-v{close_run_record.current_version_no}-"
            f"{close_run_record.period_start.isoformat()}-to-{close_run_record.period_end.isoformat()}.csv"
        )
        storage_scope = CloseRunStorageScope(
            entity_id=entity_id,
            close_run_id=close_run_id,
            period_start=close_run_record.period_start,
            period_end=close_run_record.period_end,
            close_run_version_no=close_run_record.current_version_no,
        )
        stored_artifact = self._storage_repository.store_artifact(
            scope=storage_scope,
            artifact_type=ArtifactType.GENERAL_LEDGER_EXPORT,
            idempotency_key=idempotency_key,
            filename=filename,
            payload=payload,
            content_type="text/csv; charset=utf-8",
            expected_sha256=payload_checksum,
        )

        artifact = Artifact(
            close_run_id=close_run_id,
            report_run_id=None,
            artifact_type=ArtifactType.GENERAL_LEDGER_EXPORT.value,
            storage_key=stored_artifact.reference.object_key,
            mime_type=stored_artifact.content_type,
            checksum=stored_artifact.sha256_checksum,
            idempotency_key=idempotency_key,
            version_no=close_run_record.current_version_no,
            released_at=utc_now(),
            artifact_metadata={
                **export_metadata,
                "filename": filename,
                "size_bytes": stored_artifact.size_bytes,
            },
        )
        self._db_session.add(artifact)
        try:
            self._db_session.commit()
        except IntegrityError as error:
            self._db_session.rollback()
            recovered_artifact = _load_export_artifact_by_idempotency(
                db_session=self._db_session,
                close_run_id=close_run_id,
                idempotency_key=idempotency_key,
            )
            if recovered_artifact is None:
                raise GeneralLedgerExportServiceError(
                    status_code=409,
                    code=GeneralLedgerExportServiceErrorCode.INTEGRITY_CONFLICT,
                    message=(
                        "The general-ledger export could not be recorded cleanly. Retry the "
                        "export."
                    ),
                ) from error
            return _to_general_ledger_export_summary(
                artifact=recovered_artifact,
                close_run=close_run_record,
            )

        self._db_session.refresh(artifact)
        return _to_general_ledger_export_summary(
            artifact=artifact,
            close_run=close_run_record,
        )

    def get_latest_export(
        self,
        *,
        actor_user,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> GeneralLedgerExportSummary | None:
        """Read the latest current-version GL export artifact for one close run."""

        self._verify_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        close_run_record, _entity_record = self._require_close_run_context(
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        artifact = _load_latest_export_artifact(
            db_session=self._db_session,
            close_run_id=close_run_id,
            version_no=close_run_record.current_version_no,
        )
        if artifact is None:
            return None

        return _to_general_ledger_export_summary(
            artifact=artifact,
            close_run=close_run_record,
        )

    def _verify_close_run_access(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> None:
        """Require entity membership access to the close run."""

        access_record = (
            self._db_session.query(CloseRun)
            .join(Entity, Entity.id == CloseRun.entity_id)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .filter(
                CloseRun.id == close_run_id,
                CloseRun.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
            .first()
        )
        if access_record is None:
            raise GeneralLedgerExportServiceError(
                status_code=403,
                code=GeneralLedgerExportServiceErrorCode.ACCESS_DENIED,
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
    ) -> tuple[CloseRun, Entity]:
        """Load the close run and entity rows required for export generation."""

        close_run_record = (
            self._db_session.query(CloseRun)
            .filter(CloseRun.id == close_run_id, CloseRun.entity_id == entity_id)
            .first()
        )
        if close_run_record is None:
            raise GeneralLedgerExportServiceError(
                status_code=404,
                code=GeneralLedgerExportServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="The requested close run does not exist for this entity.",
            )

        entity_record = self._db_session.query(Entity).filter(Entity.id == entity_id).first()
        if entity_record is None:
            raise GeneralLedgerExportServiceError(
                status_code=404,
                code=GeneralLedgerExportServiceErrorCode.ENTITY_NOT_FOUND,
                message="The requested entity does not exist.",
            )

        return close_run_record, entity_record


def _build_general_ledger_csv_payload(
    *,
    close_run: CloseRun,
    close_run_id: UUID,
    transactions: list[dict[str, object]],
    binding,
) -> tuple[bytes, JsonObject]:
    """Render the effective-ledger CSV payload and export metadata."""

    fieldnames = [
        "source_kind",
        "source_record_id",
        "source_line_no",
        "line_ref",
        "posting_date",
        "period",
        "account_code",
        "account_name",
        "reference",
        "external_ref",
        "description",
        "line_type",
        "debit_amount",
        "credit_amount",
        "signed_amount",
        "journal_number",
        "dimensions_json",
    ]
    ordered_transactions = sorted(
        transactions,
        key=lambda row: (
            str(row.get("date") or ""),
            str(row.get("source_kind") or ""),
            int(row.get("source_line_no") or 0),
            str(row.get("ref") or ""),
        ),
    )

    imported_line_count = sum(
        1 for row in ordered_transactions if row.get("source_kind") == "imported_general_ledger"
    )
    adjustment_line_count = sum(
        1 for row in ordered_transactions if row.get("source_kind") == "close_run_journal"
    )
    composition_mode = _resolve_composition_mode(
        imported_line_count=imported_line_count,
        adjustment_line_count=adjustment_line_count,
    )

    csv_buffer = StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in ordered_transactions:
        writer.writerow(
            {
                "source_kind": str(row.get("source_kind") or ""),
                "source_record_id": str(row.get("source_record_id") or ""),
                "source_line_no": int(row.get("source_line_no") or 0),
                "line_ref": str(row.get("ref") or ""),
                "posting_date": str(row.get("date") or ""),
                "period": str(row.get("period") or ""),
                "account_code": str(row.get("account_code") or ""),
                "account_name": str(row.get("account_name") or ""),
                "reference": str(row.get("reference") or ""),
                "external_ref": str(row.get("external_ref") or ""),
                "description": str(row.get("description") or ""),
                "line_type": str(row.get("line_type") or ""),
                "debit_amount": str(row.get("debit_amount") or ""),
                "credit_amount": str(row.get("credit_amount") or ""),
                "signed_amount": str(row.get("signed_amount") or ""),
                "journal_number": str(row.get("journal_number") or ""),
                "dimensions_json": json.dumps(row.get("dimensions") or {}, sort_keys=True),
            }
        )

    metadata: JsonObject = {
        "row_count": len(ordered_transactions),
        "imported_line_count": imported_line_count,
        "adjustment_line_count": adjustment_line_count,
        "composition_mode": composition_mode,
        "includes_imported_baseline": imported_line_count > 0,
        "bound_general_ledger_import_batch_id": (
            str(binding.general_ledger_import_batch_id)
            if binding is not None and binding.general_ledger_import_batch_id is not None
            else None
        ),
        "bound_trial_balance_import_batch_id": (
            str(binding.trial_balance_import_batch_id)
            if binding is not None and binding.trial_balance_import_batch_id is not None
            else None
        ),
        "close_run_id": str(close_run_id),
        "period_start": close_run.period_start.isoformat(),
        "period_end": close_run.period_end.isoformat(),
    }
    return csv_buffer.getvalue().encode("utf-8"), metadata


def _resolve_composition_mode(
    *,
    imported_line_count: int,
    adjustment_line_count: int,
) -> str:
    """Describe how the effective-ledger export was composed."""

    if imported_line_count > 0 and adjustment_line_count > 0:
        return "imported_gl_plus_adjustments"
    if imported_line_count > 0:
        return "imported_gl_only"
    return "adjustments_only"


def _load_export_artifact_by_idempotency(
    *,
    db_session: Session,
    close_run_id: UUID,
    idempotency_key: str,
) -> Artifact | None:
    """Return one GL export artifact by its deterministic idempotency key."""

    return (
        db_session.query(Artifact)
        .filter(
            Artifact.close_run_id == close_run_id,
            Artifact.artifact_type == ArtifactType.GENERAL_LEDGER_EXPORT.value,
            Artifact.idempotency_key == idempotency_key,
        )
        .first()
    )


def _load_latest_export_artifact(
    *,
    db_session: Session,
    close_run_id: UUID,
    version_no: int,
) -> Artifact | None:
    """Return the latest GL export artifact for the current close-run version."""

    return (
        db_session.query(Artifact)
        .filter(
            Artifact.close_run_id == close_run_id,
            Artifact.artifact_type == ArtifactType.GENERAL_LEDGER_EXPORT.value,
            Artifact.version_no == version_no,
        )
        .order_by(desc(Artifact.released_at), desc(Artifact.created_at), desc(Artifact.id))
        .first()
    )


def _to_general_ledger_export_summary(
    *,
    artifact: Artifact,
    close_run: CloseRun,
) -> GeneralLedgerExportSummary:
    """Project one artifact row into the ledger-export API contract."""

    metadata = artifact.artifact_metadata if isinstance(artifact.artifact_metadata, dict) else {}
    return GeneralLedgerExportSummary(
        artifact_id=str(artifact.id),
        close_run_id=str(close_run.id),
        period_start=close_run.period_start,
        period_end=close_run.period_end,
        version_no=artifact.version_no,
        generated_at=artifact.released_at or artifact.created_at,
        filename=str(
            metadata.get("filename")
            or f"general-ledger-export-v{artifact.version_no}.csv"
        ),
        content_type=artifact.mime_type,
        storage_key=artifact.storage_key,
        checksum=artifact.checksum,
        size_bytes=_to_int(metadata.get("size_bytes")),
        idempotency_key=artifact.idempotency_key,
        row_count=max(1, _to_int(metadata.get("row_count"))),
        imported_line_count=_to_int(metadata.get("imported_line_count")),
        adjustment_line_count=_to_int(metadata.get("adjustment_line_count")),
        composition_mode=str(metadata.get("composition_mode") or "adjustments_only"),
        includes_imported_baseline=bool(metadata.get("includes_imported_baseline")),
    )


def _to_int(value: object) -> int:
    """Convert one metadata value into an integer without raising on malformed input."""

    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


__all__ = [
    "GeneralLedgerExportService",
    "GeneralLedgerExportServiceError",
    "GeneralLedgerExportServiceErrorCode",
]
