"""
Purpose: Validate and manage canonical Step 6 standalone supporting schedules.
Scope: Typed row normalization, row mutations, review-state transitions, and
       workspace summaries for fixed assets, loan amortisation, accrual tracker,
       and budget-vs-actual workpapers.
Dependencies: Supporting-schedule repository, strict row contracts, and
       canonical supporting-schedule enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from services.common.enums import SupportingScheduleStatus, SupportingScheduleType
from services.common.types import JsonObject, utc_now
from services.contracts.supporting_schedule_models import (
    AccrualTrackerScheduleRowPayload,
    BudgetVsActualScheduleRowPayload,
    FixedAssetScheduleRowPayload,
    LoanAmortisationScheduleRowPayload,
)
from services.db.repositories.supporting_schedule_repo import (
    SupportingScheduleRecord,
    SupportingScheduleRepository,
    SupportingScheduleRowRecord,
)

_SCHEDULE_TYPE_ORDER = (
    SupportingScheduleType.FIXED_ASSETS,
    SupportingScheduleType.LOAN_AMORTISATION,
    SupportingScheduleType.ACCRUAL_TRACKER,
    SupportingScheduleType.BUDGET_VS_ACTUAL,
)


class SupportingScheduleServiceError(ValueError):
    """Represent an expected Step 6 validation or lifecycle failure."""


@dataclass(frozen=True, slots=True)
class SupportingScheduleSnapshot:
    """Describe one schedule with its persisted rows for workspace consumers."""

    schedule: SupportingScheduleRecord
    rows: tuple[SupportingScheduleRowRecord, ...]


class SupportingScheduleService:
    """Provide the single canonical business path for Step 6 schedule maintenance."""

    def __init__(self, repository: SupportingScheduleRepository) -> None:
        self._repo = repository

    def list_workspace(self, *, close_run_id: UUID) -> tuple[SupportingScheduleSnapshot, ...]:
        """Return all canonical Step 6 schedules, materializing defaults when missing."""

        existing = {
            record.schedule_type: record
            for record in self._repo.list_schedules(close_run_id=close_run_id)
        }
        snapshots: list[SupportingScheduleSnapshot] = []
        for schedule_type in _SCHEDULE_TYPE_ORDER:
            schedule = existing.get(schedule_type)
            if schedule is None:
                schedule = self._repo.get_or_create_schedule(
                    close_run_id=close_run_id,
                    schedule_type=schedule_type,
                )
            rows = tuple(self._repo.list_rows(schedule_id=schedule.id))
            snapshots.append(SupportingScheduleSnapshot(schedule=schedule, rows=rows))
        return tuple(snapshots)

    def get_schedule(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
    ) -> SupportingScheduleSnapshot:
        """Return one schedule and its rows, creating the header when missing."""

        schedule = self._repo.get_or_create_schedule(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
        )
        return SupportingScheduleSnapshot(
            schedule=schedule,
            rows=tuple(self._repo.list_rows(schedule_id=schedule.id)),
        )

    def save_row(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
        row_id: UUID | None,
        payload: dict[str, Any],
    ) -> SupportingScheduleSnapshot:
        """Create or update one schedule row and return the refreshed schedule."""

        schedule = self._repo.get_or_create_schedule(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
        )
        normalized_payload = self._normalize_payload(
            schedule_type=schedule_type,
            payload=payload,
        )
        row_ref = self._build_row_ref(schedule_type=schedule_type, payload=normalized_payload)
        self._repo.upsert_row(
            schedule_id=schedule.id,
            row_id=row_id,
            row_ref=row_ref,
            payload=normalized_payload,
        )
        refreshed = self._repo.update_schedule_status(
            schedule_id=schedule.id,
            status=SupportingScheduleStatus.IN_REVIEW,
            note=schedule.note,
            reviewed_by_user_id=None,
            reviewed_at=None,
        )
        if refreshed is None:
            raise SupportingScheduleServiceError("The supporting schedule could not be refreshed.")
        return SupportingScheduleSnapshot(
            schedule=refreshed,
            rows=tuple(self._repo.list_rows(schedule_id=refreshed.id)),
        )

    def delete_row(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
        row_id: UUID,
    ) -> SupportingScheduleSnapshot:
        """Delete one schedule row and return the refreshed schedule."""

        schedule = self._repo.get_or_create_schedule(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
        )
        deleted = self._repo.delete_row(schedule_id=schedule.id, row_id=row_id)
        if not deleted:
            raise SupportingScheduleServiceError("The supporting schedule row was not found.")
        remaining_row_count = self._repo.count_rows(schedule_id=schedule.id)
        refreshed = self._repo.update_schedule_status(
            schedule_id=schedule.id,
            status=(
                SupportingScheduleStatus.DRAFT
                if remaining_row_count == 0
                else SupportingScheduleStatus.IN_REVIEW
            ),
            note=None if remaining_row_count == 0 else schedule.note,
            reviewed_by_user_id=None,
            reviewed_at=None,
        )
        if refreshed is None:
            raise SupportingScheduleServiceError("The supporting schedule could not be refreshed.")
        return SupportingScheduleSnapshot(
            schedule=refreshed,
            rows=tuple(self._repo.list_rows(schedule_id=refreshed.id)),
        )

    def update_status(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
        status: SupportingScheduleStatus,
        note: str | None,
        actor_user_id: UUID,
    ) -> SupportingScheduleSnapshot:
        """Update one schedule review status after validating the requested transition."""

        schedule = self._repo.get_or_create_schedule(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
        )
        row_count = self._repo.count_rows(schedule_id=schedule.id)
        if status is SupportingScheduleStatus.APPROVED and row_count == 0:
            raise SupportingScheduleServiceError(
                "Add at least one row before approving this supporting schedule."
            )
        if status is SupportingScheduleStatus.NOT_APPLICABLE and not note:
            raise SupportingScheduleServiceError(
                "Provide a note when marking a supporting schedule not applicable."
            )

        reviewed_by_user_id = actor_user_id if status in {
            SupportingScheduleStatus.APPROVED,
            SupportingScheduleStatus.NOT_APPLICABLE,
        } else None
        reviewed_at = utc_now() if reviewed_by_user_id is not None else None
        refreshed = self._repo.update_schedule_status(
            schedule_id=schedule.id,
            status=status,
            note=note,
            reviewed_by_user_id=reviewed_by_user_id,
            reviewed_at=reviewed_at,
        )
        if refreshed is None:
            raise SupportingScheduleServiceError("The supporting schedule could not be updated.")
        return SupportingScheduleSnapshot(
            schedule=refreshed,
            rows=tuple(self._repo.list_rows(schedule_id=refreshed.id)),
        )

    def build_schedule_readiness(
        self,
        *,
        close_run_id: UUID,
    ) -> dict[str, dict[str, Any]]:
        """Return a compact schedule-readiness map for gates, agent, and UI surfaces."""

        workspace = self.list_workspace(close_run_id=close_run_id)
        readiness: dict[str, dict[str, Any]] = {}
        for snapshot in workspace:
            readiness[snapshot.schedule.schedule_type.value] = {
                "status": snapshot.schedule.status.value,
                "row_count": len(snapshot.rows),
                "reviewed_at": snapshot.schedule.reviewed_at,
                "note": snapshot.schedule.note,
            }
        return readiness

    def _normalize_payload(
        self,
        *,
        schedule_type: SupportingScheduleType,
        payload: dict[str, Any],
    ) -> JsonObject:
        if schedule_type is SupportingScheduleType.FIXED_ASSETS:
            fixed_asset = FixedAssetScheduleRowPayload.model_validate(payload)
            cost = _parse_decimal_string(fixed_asset.cost, field_name="cost")
            accumulated_depreciation = _parse_decimal_string(
                fixed_asset.accumulated_depreciation,
                field_name="accumulated_depreciation",
            )
            computed_nbv = cost - accumulated_depreciation
            if fixed_asset.net_book_value is not None:
                provided_nbv = _parse_decimal_string(
                    fixed_asset.net_book_value,
                    field_name="net_book_value",
                )
                if provided_nbv != computed_nbv:
                    raise SupportingScheduleServiceError(
                        "Net book value must equal cost minus accumulated depreciation."
                    )
            return {
                **fixed_asset.model_dump(exclude_none=True),
                "cost": _decimal_to_string(cost),
                "accumulated_depreciation": _decimal_to_string(accumulated_depreciation),
                "net_book_value": _decimal_to_string(computed_nbv),
                "acquisition_date": _normalize_date_string(
                    fixed_asset.acquisition_date,
                    field_name="acquisition_date",
                ),
                **(
                    {
                        "disposal_date": _normalize_date_string(
                            fixed_asset.disposal_date,
                            field_name="disposal_date",
                        )
                    }
                    if fixed_asset.disposal_date
                    else {}
                ),
                **(
                    {
                        "depreciation_expense": _decimal_to_string(
                            _parse_decimal_string(
                                fixed_asset.depreciation_expense,
                                field_name="depreciation_expense",
                            )
                        )
                    }
                    if fixed_asset.depreciation_expense is not None
                    else {}
                ),
            }

        if schedule_type is SupportingScheduleType.LOAN_AMORTISATION:
            loan = LoanAmortisationScheduleRowPayload.model_validate(payload)
            return {
                **loan.model_dump(exclude_none=True),
                "due_date": _normalize_date_string(loan.due_date, field_name="due_date"),
                "principal": _decimal_to_string(
                    _parse_decimal_string(loan.principal, field_name="principal")
                ),
                "interest": _decimal_to_string(
                    _parse_decimal_string(loan.interest, field_name="interest")
                ),
                "balance": _decimal_to_string(
                    _parse_decimal_string(loan.balance, field_name="balance")
                ),
            }

        if schedule_type is SupportingScheduleType.ACCRUAL_TRACKER:
            accrual = AccrualTrackerScheduleRowPayload.model_validate(payload)
            return {
                **accrual.model_dump(exclude_none=True),
                "amount": _decimal_to_string(
                    _parse_decimal_string(accrual.amount, field_name="amount")
                ),
                "period": _normalize_period_string(accrual.period, field_name="period"),
                **(
                    {
                        "reversal_date": _normalize_date_string(
                            accrual.reversal_date,
                            field_name="reversal_date",
                        )
                    }
                    if accrual.reversal_date
                    else {}
                ),
            }

        budget = BudgetVsActualScheduleRowPayload.model_validate(payload)
        return {
            **budget.model_dump(exclude_none=True),
            "budget_amount": _decimal_to_string(
                _parse_decimal_string(budget.budget_amount, field_name="budget_amount")
            ),
            "period": _normalize_period_string(budget.period, field_name="period"),
        }

    @staticmethod
    def _build_row_ref(
        *,
        schedule_type: SupportingScheduleType,
        payload: JsonObject,
    ) -> str:
        if schedule_type is SupportingScheduleType.FIXED_ASSETS:
            return f"asset:{payload['asset_id']}"
        if schedule_type is SupportingScheduleType.LOAN_AMORTISATION:
            return f"loan:{payload['loan_id']}:payment:{payload['payment_no']}"
        if schedule_type is SupportingScheduleType.ACCRUAL_TRACKER:
            return f"accrual:{payload['ref']}"

        cost_centre = str(payload.get("cost_centre") or "").strip()
        department = str(payload.get("department") or "").strip()
        project = str(payload.get("project") or "").strip()
        dimension_suffix = ":".join(
            dimension
            for dimension in (department, cost_centre, project)
            if dimension
        )
        if dimension_suffix:
            return f"budget:{payload['account_code']}:{payload['period']}:{dimension_suffix}"
        return f"budget:{payload['account_code']}:{payload['period']}"


def _parse_decimal_string(value: str | None, *, field_name: str) -> Decimal:
    if value is None:
        raise SupportingScheduleServiceError(f"{field_name} is required.")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise SupportingScheduleServiceError(
            f"{field_name} must be a valid decimal string."
        ) from error
    return parsed.quantize(Decimal("0.01"))


def _decimal_to_string(value: Decimal) -> str:
    return f"{value:.2f}"


def _normalize_date_string(value: str, *, field_name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise SupportingScheduleServiceError(
            f"{field_name} must be a valid YYYY-MM-DD date."
        ) from error


def _normalize_period_string(value: str, *, field_name: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError as error:
        raise SupportingScheduleServiceError(
            f"{field_name} must be a valid YYYY-MM period."
        ) from error
    return parsed.strftime("%Y-%m")


__all__ = [
    "SupportingScheduleService",
    "SupportingScheduleServiceError",
    "SupportingScheduleSnapshot",
]
