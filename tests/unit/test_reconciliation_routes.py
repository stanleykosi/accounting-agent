"""
Purpose: Verify reconciliation route helpers stay aligned with current applicability rules.
Scope: Focused unit coverage for run-queue applicability and list behavior only.
Dependencies: Reconciliation API route helpers plus lightweight doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from apps.api.app.routes import reconciliation as reconciliation_route
from services.common.enums import (
    ReconciliationStatus,
    ReconciliationType,
    SupportingScheduleStatus,
    SupportingScheduleType,
)
from services.db.repositories.reconciliation_repo import ReconciliationRecord


class _FakeScalarResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _FakeDbSession:
    def __init__(self, *, approved_bank_statement_count: int = 0) -> None:
        self.approved_bank_statement_count = approved_bank_statement_count

    def execute(self, statement):
        del statement
        return _FakeScalarResult(self.approved_bank_statement_count)


def test_resolve_requested_reconciliation_types_skips_not_applicable_schedule_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows parked as not applicable should not be treated as runnable reconciliation work."""

    close_run_id = uuid4()
    monkeypatch.setattr(
        reconciliation_route,
        "load_effective_ledger_transactions",
        lambda session, close_run_id: [],
    )
    monkeypatch.setattr(
        reconciliation_route,
        "load_close_run_ledger_binding",
        lambda session, close_run_id: None,
    )
    monkeypatch.setattr(
        reconciliation_route,
        "SupportingScheduleService",
        lambda repository: SimpleNamespace(
            list_workspace=lambda close_run_id: (
                SimpleNamespace(
                    schedule=SimpleNamespace(
                        schedule_type=SupportingScheduleType.FIXED_ASSETS,
                        status=SupportingScheduleStatus.NOT_APPLICABLE,
                    ),
                    rows=(SimpleNamespace(payload={"asset_id": "FA-001"}),),
                ),
            )
        ),
    )

    applicable, skipped, message = reconciliation_route._resolve_requested_reconciliation_types(
        close_run_id=close_run_id,
        reconciliation_types=(ReconciliationType.FIXED_ASSETS,),
        db_session=_FakeDbSession(),
    )

    assert applicable == ()
    assert skipped == (ReconciliationType.FIXED_ASSETS,)
    assert message == reconciliation_route.NO_APPLICABLE_RECONCILIATION_WORK_MESSAGE


def test_list_reconciliations_keeps_existing_rows_visible_when_inputs_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Previously created reconciliation runs should still be visible for review."""

    close_run_id = uuid4()
    record = ReconciliationRecord(
        id=uuid4(),
        close_run_id=close_run_id,
        reconciliation_type=ReconciliationType.FIXED_ASSETS,
        status=ReconciliationStatus.IN_REVIEW,
        summary={"total_items": 1, "exception_count": 1},
        blocking_reason="Awaiting reviewer disposition.",
        approved_by_user_id=None,
        created_by_user_id=None,
        created_at=datetime(2026, 4, 19, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 19, 10, 5, tzinfo=UTC),
    )

    monkeypatch.setattr(
        reconciliation_route,
        "_require_close_run_access",
        lambda **kwargs: (SimpleNamespace(id=uuid4(), email="ops@example.com", full_name="Ops"), True),
    )
    monkeypatch.setattr(
        reconciliation_route,
        "_resolve_requested_reconciliation_types",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("list_reconciliations should not filter existing rows by applicability")
        ),
    )

    response = reconciliation_route.list_reconciliations(
        entity_id=uuid4(),
        close_run_id=close_run_id,
        request=SimpleNamespace(),
        response=SimpleNamespace(),
        settings=SimpleNamespace(),
        auth_service=SimpleNamespace(),
        db_session=_FakeDbSession(),
        auth_context=SimpleNamespace(user=SimpleNamespace(id=uuid4())),
        reconciliation_service=SimpleNamespace(
            list_reconciliations=lambda close_run_id: [record]
        ),
    )

    assert len(response.reconciliations) == 1
    assert response.reconciliations[0].id == str(record.id)
    assert response.reconciliations[0].reconciliation_type is ReconciliationType.FIXED_ASSETS
