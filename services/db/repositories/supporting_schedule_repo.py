"""
Purpose: Persist and query standalone Step 6 supporting schedules and rows.
Scope: Header creation, row upserts, review-status mutations, and list/read
       operations used by the API, agent, and reconciliation loader.
Dependencies: SQLAlchemy sessions and supporting-schedule ORM models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from services.common.enums import SupportingScheduleStatus, SupportingScheduleType
from services.common.types import JsonObject
from services.db.models.supporting_schedules import SupportingSchedule, SupportingScheduleRow
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class SupportingScheduleRecord:
    """Describe one persisted supporting-schedule header."""

    id: UUID
    close_run_id: UUID
    schedule_type: SupportingScheduleType
    status: SupportingScheduleStatus
    note: str | None
    reviewed_by_user_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SupportingScheduleRowRecord:
    """Describe one persisted supporting-schedule row."""

    id: UUID
    supporting_schedule_id: UUID
    row_ref: str
    line_no: int
    payload: JsonObject
    created_at: datetime
    updated_at: datetime


class SupportingScheduleRepository:
    """Provide the canonical persistence layer for Step 6 supporting schedules."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_schedules(self, *, close_run_id: UUID) -> list[SupportingScheduleRecord]:
        stmt = (
            select(SupportingSchedule)
            .where(SupportingSchedule.close_run_id == close_run_id)
            .order_by(SupportingSchedule.schedule_type)
        )
        return [self._to_schedule_record(row) for row in self._session.scalars(stmt).all()]

    def get_schedule(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
    ) -> SupportingScheduleRecord | None:
        stmt = select(SupportingSchedule).where(
            SupportingSchedule.close_run_id == close_run_id,
            SupportingSchedule.schedule_type == schedule_type.value,
        )
        row = self._session.scalar(stmt)
        if row is None:
            return None
        return self._to_schedule_record(row)

    def get_schedule_by_id(self, *, schedule_id: UUID) -> SupportingScheduleRecord | None:
        row = self._session.get(SupportingSchedule, schedule_id)
        if row is None:
            return None
        return self._to_schedule_record(row)

    def get_or_create_schedule(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
    ) -> SupportingScheduleRecord:
        existing = self.get_schedule(close_run_id=close_run_id, schedule_type=schedule_type)
        if existing is not None:
            return existing
        row = SupportingSchedule(
            close_run_id=close_run_id,
            schedule_type=schedule_type.value,
            status=SupportingScheduleStatus.DRAFT.value,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_schedule_record(row)

    def update_schedule_status(
        self,
        *,
        schedule_id: UUID,
        status: SupportingScheduleStatus,
        note: str | None,
        reviewed_by_user_id: UUID | None,
        reviewed_at: datetime | None,
    ) -> SupportingScheduleRecord | None:
        row = self._session.get(SupportingSchedule, schedule_id)
        if row is None:
            return None
        row.status = status.value
        row.note = note
        row.reviewed_by_user_id = reviewed_by_user_id
        row.reviewed_at = reviewed_at
        self._session.flush()
        return self._to_schedule_record(row)

    def list_rows(self, *, schedule_id: UUID) -> list[SupportingScheduleRowRecord]:
        stmt = (
            select(SupportingScheduleRow)
            .where(SupportingScheduleRow.supporting_schedule_id == schedule_id)
            .order_by(SupportingScheduleRow.line_no, SupportingScheduleRow.created_at)
        )
        return [self._to_row_record(row) for row in self._session.scalars(stmt).all()]

    def get_row(
        self,
        *,
        schedule_id: UUID,
        row_id: UUID,
    ) -> SupportingScheduleRowRecord | None:
        stmt = select(SupportingScheduleRow).where(
            SupportingScheduleRow.supporting_schedule_id == schedule_id,
            SupportingScheduleRow.id == row_id,
        )
        row = self._session.scalar(stmt)
        if row is None:
            return None
        return self._to_row_record(row)

    def upsert_row(
        self,
        *,
        schedule_id: UUID,
        row_id: UUID | None,
        row_ref: str,
        payload: JsonObject,
    ) -> SupportingScheduleRowRecord:
        if row_id is not None:
            row = self._session.get(SupportingScheduleRow, row_id)
            if row is None or row.supporting_schedule_id != schedule_id:
                raise ValueError("The supporting schedule row does not exist in this schedule.")
            row.row_ref = row_ref
            row.payload = dict(payload)
            self._session.flush()
            return self._to_row_record(row)

        max_line_no = self._session.execute(
            select(func.max(SupportingScheduleRow.line_no)).where(
                SupportingScheduleRow.supporting_schedule_id == schedule_id
            )
        ).scalar_one()
        row = SupportingScheduleRow(
            supporting_schedule_id=schedule_id,
            row_ref=row_ref,
            line_no=(int(max_line_no or 0) + 1),
            payload=dict(payload),
        )
        self._session.add(row)
        self._session.flush()
        return self._to_row_record(row)

    def delete_row(self, *, schedule_id: UUID, row_id: UUID) -> bool:
        row = self._session.get(SupportingScheduleRow, row_id)
        if row is None or row.supporting_schedule_id != schedule_id:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def count_rows(self, *, schedule_id: UUID) -> int:
        return int(
            self._session.execute(
                select(func.count(SupportingScheduleRow.id)).where(
                    SupportingScheduleRow.supporting_schedule_id == schedule_id
                )
            ).scalar_one()
        )

    @staticmethod
    def _to_schedule_record(row: SupportingSchedule) -> SupportingScheduleRecord:
        return SupportingScheduleRecord(
            id=row.id,
            close_run_id=row.close_run_id,
            schedule_type=SupportingScheduleType(row.schedule_type),
            status=SupportingScheduleStatus(row.status),
            note=row.note,
            reviewed_by_user_id=row.reviewed_by_user_id,
            reviewed_at=row.reviewed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_row_record(row: SupportingScheduleRow) -> SupportingScheduleRowRecord:
        return SupportingScheduleRowRecord(
            id=row.id,
            supporting_schedule_id=row.supporting_schedule_id,
            row_ref=row.row_ref,
            line_no=row.line_no,
            payload=dict(row.payload),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


__all__ = [
    "SupportingScheduleRecord",
    "SupportingScheduleRepository",
    "SupportingScheduleRowRecord",
]
