"""
Purpose: Verify the standalone Step 6 supporting-schedule service.
Scope: Typed row normalization, review-state validation, and workspace readiness
coverage without a live database.
Dependencies: SupportingScheduleService and lightweight repository doubles.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from services.common.enums import SupportingScheduleStatus, SupportingScheduleType
from services.db.repositories.supporting_schedule_repo import (
    SupportingScheduleRecord,
    SupportingScheduleRowRecord,
)
from services.supporting_schedules.service import (
    SupportingScheduleService,
    SupportingScheduleServiceError,
)


def test_fixed_asset_row_is_normalized_and_sets_schedule_in_review() -> None:
    """Saving a fixed-asset row should compute NBV and reopen the schedule for review."""

    repository = InMemorySupportingScheduleRepository()
    service = SupportingScheduleService(repository=repository)
    close_run_id = UUID("10000000-0000-0000-0000-000000000001")

    snapshot = service.save_row(
        close_run_id=close_run_id,
        schedule_type=SupportingScheduleType.FIXED_ASSETS,
        row_id=None,
        payload={
            "asset_id": "FA-001",
            "asset_name": "Office laptops",
            "acquisition_date": "2026-03-01",
            "asset_account_code": "1500",
            "accumulated_depreciation_account_code": "1510",
            "cost": "125000.00",
            "accumulated_depreciation": "25000.00",
        },
    )

    assert snapshot.schedule.status is SupportingScheduleStatus.IN_REVIEW
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].payload["net_book_value"] == "100000.00"


def test_schedule_status_requires_rows_or_not_applicable_note() -> None:
    """Approvals require rows, and not-applicable decisions require a note."""

    repository = InMemorySupportingScheduleRepository()
    service = SupportingScheduleService(repository=repository)
    close_run_id = UUID("20000000-0000-0000-0000-000000000001")
    actor_user_id = UUID("30000000-0000-0000-0000-000000000001")

    with pytest.raises(
        SupportingScheduleServiceError,
        match="Add at least one row before approving",
    ):
        service.update_status(
            close_run_id=close_run_id,
            schedule_type=SupportingScheduleType.LOAN_AMORTISATION,
            status=SupportingScheduleStatus.APPROVED,
            note=None,
            actor_user_id=actor_user_id,
        )

    with pytest.raises(
        SupportingScheduleServiceError,
        match="Provide a note when marking a supporting schedule not applicable",
    ):
        service.update_status(
            close_run_id=close_run_id,
            schedule_type=SupportingScheduleType.LOAN_AMORTISATION,
            status=SupportingScheduleStatus.NOT_APPLICABLE,
            note=None,
            actor_user_id=actor_user_id,
        )


def test_workspace_materializes_all_four_canonical_schedules() -> None:
    """The Step 6 workspace should always expose the full supporting-schedule set."""

    repository = InMemorySupportingScheduleRepository()
    service = SupportingScheduleService(repository=repository)
    close_run_id = UUID("40000000-0000-0000-0000-000000000001")

    readiness = service.build_schedule_readiness(close_run_id=close_run_id)

    assert tuple(readiness) == (
        "fixed_assets",
        "loan_amortisation",
        "accrual_tracker",
        "budget_vs_actual",
    )
    assert all(state["status"] == "draft" for state in readiness.values())
    assert all(state["row_count"] == 0 for state in readiness.values())


def test_deleting_last_schedule_row_returns_schedule_to_draft() -> None:
    """Empty schedules should stop blocking review once their last row is removed."""

    repository = InMemorySupportingScheduleRepository()
    service = SupportingScheduleService(repository=repository)
    close_run_id = UUID("50000000-0000-0000-0000-000000000001")

    created = service.save_row(
        close_run_id=close_run_id,
        schedule_type=SupportingScheduleType.ACCRUAL_TRACKER,
        row_id=None,
        payload={
            "ref": "AC-001",
            "description": "Year-end utilities accrual",
            "account_code": "2200",
            "amount": "950.00",
            "period": "2026-03",
            "reversal_date": "2026-04-01",
        },
    )

    refreshed = service.delete_row(
        close_run_id=close_run_id,
        schedule_type=SupportingScheduleType.ACCRUAL_TRACKER,
        row_id=created.rows[0].id,
    )

    assert refreshed.schedule.status is SupportingScheduleStatus.DRAFT
    assert refreshed.schedule.note is None
    assert refreshed.rows == ()


class InMemorySupportingScheduleRepository:
    """Provide a deterministic repository double for Step 6 service tests."""

    def __init__(self) -> None:
        self._schedule_ids_by_type: dict[tuple[UUID, SupportingScheduleType], UUID] = {}
        self._schedules: dict[UUID, SupportingScheduleRecord] = {}
        self._rows_by_schedule_id: dict[UUID, list[SupportingScheduleRowRecord]] = {}

    def list_schedules(self, *, close_run_id: UUID) -> list[SupportingScheduleRecord]:
        return [
            schedule
            for schedule in self._schedules.values()
            if schedule.close_run_id == close_run_id
        ]

    def get_schedule(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
    ) -> SupportingScheduleRecord | None:
        schedule_id = self._schedule_ids_by_type.get((close_run_id, schedule_type))
        return self._schedules.get(schedule_id) if schedule_id is not None else None

    def get_or_create_schedule(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
    ) -> SupportingScheduleRecord:
        existing = self.get_schedule(close_run_id=close_run_id, schedule_type=schedule_type)
        if existing is not None:
            return existing
        now = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        schedule = SupportingScheduleRecord(
            id=uuid4(),
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            status=SupportingScheduleStatus.DRAFT,
            note=None,
            reviewed_by_user_id=None,
            reviewed_at=None,
            created_at=now,
            updated_at=now,
        )
        self._schedule_ids_by_type[(close_run_id, schedule_type)] = schedule.id
        self._schedules[schedule.id] = schedule
        self._rows_by_schedule_id[schedule.id] = []
        return schedule

    def update_schedule_status(
        self,
        *,
        schedule_id: UUID,
        status: SupportingScheduleStatus,
        note: str | None,
        reviewed_by_user_id: UUID | None,
        reviewed_at: datetime | None,
    ) -> SupportingScheduleRecord | None:
        schedule = self._schedules.get(schedule_id)
        if schedule is None:
            return None
        updated = replace(
            schedule,
            status=status,
            note=note,
            reviewed_by_user_id=reviewed_by_user_id,
            reviewed_at=reviewed_at,
            updated_at=datetime(2026, 4, 15, 12, 5, tzinfo=UTC),
        )
        self._schedules[schedule_id] = updated
        return updated

    def list_rows(self, *, schedule_id: UUID) -> list[SupportingScheduleRowRecord]:
        return list(self._rows_by_schedule_id.get(schedule_id, []))

    def upsert_row(
        self,
        *,
        schedule_id: UUID,
        row_id: UUID | None,
        row_ref: str,
        payload: dict[str, object],
    ) -> SupportingScheduleRowRecord:
        rows = self._rows_by_schedule_id.setdefault(schedule_id, [])
        now = datetime(2026, 4, 15, 12, 10, tzinfo=UTC)
        if row_id is not None:
            for index, row in enumerate(rows):
                if row.id == row_id:
                    updated = replace(
                        row,
                        row_ref=row_ref,
                        payload=payload,
                        updated_at=now,
                    )
                    rows[index] = updated
                    return updated
            raise ValueError("The supporting schedule row does not exist in this schedule.")
        created = SupportingScheduleRowRecord(
            id=uuid4(),
            supporting_schedule_id=schedule_id,
            row_ref=row_ref,
            line_no=len(rows) + 1,
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        rows.append(created)
        return created

    def delete_row(self, *, schedule_id: UUID, row_id: UUID) -> bool:
        rows = self._rows_by_schedule_id.get(schedule_id, [])
        for index, row in enumerate(rows):
            if row.id == row_id:
                rows.pop(index)
                return True
        return False

    def count_rows(self, *, schedule_id: UUID) -> int:
        return len(self._rows_by_schedule_id.get(schedule_id, []))
