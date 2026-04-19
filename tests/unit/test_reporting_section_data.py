"""
Purpose: Verify deterministic report-section data loading from close-run ledger sources.
Scope: Budget variance, KPI dashboard, and balance-backed reporting metrics only.
Dependencies: Reporting section-data helpers plus in-memory SQLite persistence doubles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from services.common.enums import ReviewStatus, SupportingScheduleStatus, SupportingScheduleType
from services.db.base import Base
from services.db.models.close_run import CloseRun
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ledger import (
    CloseRunLedgerBinding,
    GeneralLedgerImportBatch,
    GeneralLedgerImportLine,
    TrialBalanceImportBatch,
    TrialBalanceImportLine,
)
from services.db.repositories.supporting_schedule_repo import (
    SupportingScheduleRecord,
    SupportingScheduleRowRecord,
)
from services.reporting import section_data as section_data_module
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(
    _type_: JSONB,
    _compiler: object,
    **_compiler_kwargs: object,
) -> str:
    """Allow reporting section-data helpers to run against in-memory SQLite."""

    return "JSON"


@dataclass(frozen=True, slots=True)
class _BudgetScheduleFixture:
    schedule: SupportingScheduleRecord
    rows: tuple[SupportingScheduleRowRecord, ...]


class _FakeSupportingScheduleRepository:
    def __init__(self, session) -> None:
        del session

    def get_schedule(
        self,
        *,
        close_run_id: UUID,
        schedule_type: SupportingScheduleType,
    ) -> SupportingScheduleRecord | None:
        del schedule_type
        fixture = _BUDGET_FIXTURES.get(close_run_id)
        return fixture.schedule if fixture is not None else None

    def list_rows(self, *, schedule_id: UUID) -> list[SupportingScheduleRowRecord]:
        for fixture in _BUDGET_FIXTURES.values():
            if fixture.schedule.id == schedule_id:
                return list(fixture.rows)
        return []


_BUDGET_FIXTURES: dict[UUID, _BudgetScheduleFixture] = {}


def _build_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            CloseRun.__table__,
            GeneralLedgerImportBatch.__table__,
            GeneralLedgerImportLine.__table__,
            TrialBalanceImportBatch.__table__,
            TrialBalanceImportLine.__table__,
            CloseRunLedgerBinding.__table__,
            JournalEntry.__table__,
            JournalLine.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _add_journal(
    session,
    *,
    entity_id: UUID,
    close_run_id: UUID,
    journal_number: str,
    posting_date: date,
    description: str,
    lines: tuple[dict[str, object], ...],
) -> None:
    debit_total = sum(
        float(line["amount"]) for line in lines if line["line_type"] == "debit"
    )
    credit_total = sum(
        float(line["amount"]) for line in lines if line["line_type"] == "credit"
    )
    journal_id = uuid4()
    session.add(
        JournalEntry(
            id=journal_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            recommendation_id=None,
            journal_number=journal_number,
            posting_date=posting_date,
            status=ReviewStatus.APPROVED.value,
            description=description,
            total_debits=f"{debit_total:.2f}",
            total_credits=f"{credit_total:.2f}",
            line_count=len(lines),
            source_surface="system",
            autonomy_mode=None,
            reasoning_summary=None,
            metadata_payload={},
            approved_by_user_id=None,
            applied_by_user_id=None,
            superseded_by_id=None,
        )
    )
    session.add_all(
        JournalLine(
            id=uuid4(),
            journal_entry_id=journal_id,
            line_no=index,
            account_code=str(line["account_code"]),
            line_type=str(line["line_type"]),
            amount=str(line["amount"]),
            description=str(line.get("description") or ""),
            dimensions=dict(line.get("dimensions") or {}),
            reference=str(line.get("reference") or ""),
        )
        for index, line in enumerate(lines, start=1)
    )


def _seed_close_run(
    session,
    *,
    close_run_id: UUID,
    entity_id: UUID,
    period_start: date,
    period_end: date,
) -> None:
    session.add(
        CloseRun(
            id=close_run_id,
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            status="draft",
            reporting_currency="USD",
            current_version_no=1,
            opened_by_user_id=uuid4(),
            approved_by_user_id=None,
            approved_at=None,
            archived_at=None,
            reopened_from_close_run_id=None,
        )
    )


def _seed_trial_balance(
    session,
    *,
    close_run_id: UUID,
    entity_id: UUID,
    period_start: date,
    period_end: date,
    cash_balance: str,
    inventory_balance: str,
    liability_balance: str,
    equity_balance: str,
) -> None:
    batch_id = uuid4()
    session.add(
        TrialBalanceImportBatch(
            id=batch_id,
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            source_format="csv",
            uploaded_filename="tb.csv",
            row_count=4,
            imported_by_user_id=None,
            import_metadata={},
        )
    )
    session.add_all(
        (
            TrialBalanceImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=1,
                account_code="1000",
                account_name="Bank Account",
                account_type="asset",
                debit_balance=cash_balance,
                credit_balance="0.00",
                is_active=True,
            ),
            TrialBalanceImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=2,
                account_code="1400",
                account_name="Inventory",
                account_type="asset",
                debit_balance=inventory_balance,
                credit_balance="0.00",
                is_active=True,
            ),
            TrialBalanceImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=3,
                account_code="2000",
                account_name="Accounts Payable",
                account_type="liability",
                debit_balance="0.00",
                credit_balance=liability_balance,
                is_active=True,
            ),
            TrialBalanceImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=4,
                account_code="3000",
                account_name="Owner Equity",
                account_type="equity",
                debit_balance="0.00",
                credit_balance=equity_balance,
                is_active=True,
            ),
        )
    )
    session.add(
        CloseRunLedgerBinding(
            id=uuid4(),
            close_run_id=close_run_id,
            general_ledger_import_batch_id=None,
            trial_balance_import_batch_id=batch_id,
            binding_source="auto",
            bound_by_user_id=None,
        )
    )


def test_load_budget_variance_data_uses_approved_rows_and_dimension_filtered_actuals(
    monkeypatch,
) -> None:
    """Budget actuals should respect both approved schedule rows and ledger dimensions."""

    _BUDGET_FIXTURES.clear()
    session = _build_session()
    entity_id = uuid4()
    close_run_id = uuid4()
    _seed_close_run(
        session,
        close_run_id=close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        journal_number="JE-2026-00001",
        posting_date=date(2026, 3, 12),
        description="Department expenses",
        lines=(
            {
                "account_code": "6100",
                "line_type": "debit",
                "amount": "120.00",
                "dimensions": {"department": "Ops"},
            },
            {
                "account_code": "6100",
                "line_type": "debit",
                "amount": "80.00",
                "dimensions": {"department": "Sales"},
            },
            {
                "account_code": "1000",
                "line_type": "credit",
                "amount": "200.00",
                "dimensions": {},
            },
        ),
    )
    session.commit()

    monkeypatch.setattr(
        section_data_module,
        "load_active_coa_accounts",
        lambda session, close_run_id: {
            "1000": {
                "account_code": "1000",
                "account_name": "Bank Account",
                "account_type": "asset",
                "is_postable": True,
            },
            "6100": {
                "account_code": "6100",
                "account_name": "Office Expense",
                "account_type": "expense",
                "is_postable": True,
            },
        },
    )
    monkeypatch.setattr(
        section_data_module,
        "SupportingScheduleRepository",
        _FakeSupportingScheduleRepository,
    )
    _BUDGET_FIXTURES[close_run_id] = _BudgetScheduleFixture(
        schedule=SupportingScheduleRecord(
            id=uuid4(),
            close_run_id=close_run_id,
            schedule_type=SupportingScheduleType.BUDGET_VS_ACTUAL,
            status=SupportingScheduleStatus.APPROVED,
            note=None,
            reviewed_by_user_id=None,
            reviewed_at=None,
            created_at=session.get(CloseRun, close_run_id).created_at,
            updated_at=session.get(CloseRun, close_run_id).updated_at,
        ),
        rows=(
            SupportingScheduleRowRecord(
                id=uuid4(),
                supporting_schedule_id=uuid4(),
                row_ref="6100:2026-03:ops",
                line_no=1,
                payload={
                    "account_code": "6100",
                    "period": "2026-03",
                    "budget_amount": "150.00",
                    "department": "Ops",
                },
                created_at=session.get(CloseRun, close_run_id).created_at,
                updated_at=session.get(CloseRun, close_run_id).updated_at,
            ),
            SupportingScheduleRowRecord(
                id=uuid4(),
                supporting_schedule_id=uuid4(),
                row_ref="6100:2026-03:all",
                line_no=2,
                payload={
                    "account_code": "6100",
                    "period": "2026-03",
                    "budget_amount": "300.00",
                },
                created_at=session.get(CloseRun, close_run_id).created_at,
                updated_at=session.get(CloseRun, close_run_id).updated_at,
            ),
        ),
    )

    budget_data = section_data_module.load_budget_variance_data(session, close_run_id)

    assert len(budget_data["items"]) == 2
    ops_item = next(
        item for item in budget_data["items"] if "Department: Ops" in item["label"]
    )
    total_item = next(
        item for item in budget_data["items"] if item["label"] == "6100 Office Expense"
    )
    assert ops_item["actual"] == 120
    assert total_item["actual"] == 200


def test_load_kpi_dashboard_data_uses_current_and_prior_close_run_facts(monkeypatch) -> None:
    """KPI metrics should derive from deterministic ledger and TB-backed balances."""

    _BUDGET_FIXTURES.clear()
    session = _build_session()
    entity_id = uuid4()
    prior_close_run_id = uuid4()
    current_close_run_id = uuid4()
    _seed_close_run(
        session,
        close_run_id=prior_close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
    )
    _seed_close_run(
        session,
        close_run_id=current_close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    _seed_trial_balance(
        session,
        close_run_id=prior_close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
        cash_balance="110.00",
        inventory_balance="810.00",
        liability_balance="600.00",
        equity_balance="320.00",
    )
    _seed_trial_balance(
        session,
        close_run_id=current_close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        cash_balance="100.00",
        inventory_balance="1000.00",
        liability_balance="700.00",
        equity_balance="400.00",
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=prior_close_run_id,
        journal_number="JE-2026-00010",
        posting_date=date(2026, 2, 10),
        description="Prior-period sale",
        lines=(
            {"account_code": "1000", "line_type": "debit", "amount": "800.00"},
            {"account_code": "4000", "line_type": "credit", "amount": "800.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=prior_close_run_id,
        journal_number="JE-2026-00011",
        posting_date=date(2026, 2, 11),
        description="Prior-period cost of sales",
        lines=(
            {"account_code": "5000", "line_type": "debit", "amount": "320.00"},
            {"account_code": "1400", "line_type": "credit", "amount": "320.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=prior_close_run_id,
        journal_number="JE-2026-00012",
        posting_date=date(2026, 2, 12),
        description="Prior-period expenses",
        lines=(
            {"account_code": "6100", "line_type": "debit", "amount": "160.00"},
            {"account_code": "1000", "line_type": "credit", "amount": "160.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=current_close_run_id,
        journal_number="JE-2026-00020",
        posting_date=date(2026, 3, 10),
        description="Current-period sale",
        lines=(
            {"account_code": "1000", "line_type": "debit", "amount": "1000.00"},
            {"account_code": "4000", "line_type": "credit", "amount": "1000.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=current_close_run_id,
        journal_number="JE-2026-00021",
        posting_date=date(2026, 3, 11),
        description="Current-period cost of sales",
        lines=(
            {"account_code": "5000", "line_type": "debit", "amount": "400.00"},
            {"account_code": "1400", "line_type": "credit", "amount": "400.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=current_close_run_id,
        journal_number="JE-2026-00022",
        posting_date=date(2026, 3, 12),
        description="Current-period expenses",
        lines=(
            {"account_code": "6100", "line_type": "debit", "amount": "200.00"},
            {"account_code": "1000", "line_type": "credit", "amount": "200.00"},
        ),
    )
    session.commit()

    monkeypatch.setattr(
        section_data_module,
        "load_active_coa_accounts",
        lambda session, close_run_id: {
            "1000": {
                "account_code": "1000",
                "account_name": "Bank Account",
                "account_type": "asset",
                "is_postable": True,
            },
            "1400": {
                "account_code": "1400",
                "account_name": "Inventory",
                "account_type": "asset",
                "is_postable": True,
            },
            "2000": {
                "account_code": "2000",
                "account_name": "Accounts Payable",
                "account_type": "liability",
                "is_postable": True,
            },
            "3000": {
                "account_code": "3000",
                "account_name": "Owner Equity",
                "account_type": "equity",
                "is_postable": True,
            },
            "4000": {
                "account_code": "4000",
                "account_name": "Sales Revenue",
                "account_type": "revenue",
                "is_postable": True,
            },
            "5000": {
                "account_code": "5000",
                "account_name": "Cost of Sales",
                "account_type": "cost_of_sales",
                "is_postable": True,
            },
            "6100": {
                "account_code": "6100",
                "account_name": "Operating Expense",
                "account_type": "expense",
                "is_postable": True,
            },
        },
    )
    monkeypatch.setattr(
        section_data_module,
        "SupportingScheduleRepository",
        _FakeSupportingScheduleRepository,
    )
    _BUDGET_FIXTURES.clear()

    kpi_data = section_data_module.load_kpi_dashboard_data(session, current_close_run_id)

    metrics = {metric["label"]: metric for metric in kpi_data["metrics"]}
    assert metrics["Revenue"]["value"] == 1000
    assert metrics["Revenue"]["prior_period"] == 800
    assert metrics["Revenue"]["change"] == 200
    assert metrics["Gross Margin"]["value"] == "60.0%"
    assert metrics["Net Margin"]["value"] == "40.0%"
    assert metrics["Debt-to-Equity"]["value"] == "0.88x"
    assert metrics["Closing Cash Balance"]["value"] == 900


def test_gather_report_section_data_builds_cash_flow_from_cash_movements(monkeypatch) -> None:
    """Cash flow should classify bank movements into operating, investing, and financing."""

    _BUDGET_FIXTURES.clear()
    session = _build_session()
    entity_id = uuid4()
    close_run_id = uuid4()
    _seed_close_run(
        session,
        close_run_id=close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        journal_number="JE-2026-00030",
        posting_date=date(2026, 3, 5),
        description="Customer receipt",
        lines=(
            {"account_code": "1000", "line_type": "debit", "amount": "500.00"},
            {"account_code": "4000", "line_type": "credit", "amount": "500.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        journal_number="JE-2026-00031",
        posting_date=date(2026, 3, 8),
        description="Equipment purchase",
        lines=(
            {"account_code": "1500", "line_type": "debit", "amount": "100.00"},
            {"account_code": "1000", "line_type": "credit", "amount": "100.00"},
        ),
    )
    _add_journal(
        session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        journal_number="JE-2026-00032",
        posting_date=date(2026, 3, 12),
        description="Loan drawdown",
        lines=(
            {"account_code": "1000", "line_type": "debit", "amount": "200.00"},
            {"account_code": "2100", "line_type": "credit", "amount": "200.00"},
        ),
    )
    session.commit()

    monkeypatch.setattr(
        section_data_module,
        "load_active_coa_accounts",
        lambda session, close_run_id: {
            "1000": {
                "account_code": "1000",
                "account_name": "Bank Account",
                "account_type": "asset",
                "is_postable": True,
            },
            "1500": {
                "account_code": "1500",
                "account_name": "Office Equipment",
                "account_type": "asset",
                "is_postable": True,
            },
            "2100": {
                "account_code": "2100",
                "account_name": "Bank Loan",
                "account_type": "liability",
                "is_postable": True,
            },
            "4000": {
                "account_code": "4000",
                "account_name": "Sales Revenue",
                "account_type": "revenue",
                "is_postable": True,
            },
        },
    )

    section_data = section_data_module.gather_report_section_data(
        session,
        close_run_id,
        ["cash_flow"],
    )
    cash_flow = section_data["cash_flow"]

    assert cash_flow["operating_activities"] == {"4000 Sales Revenue": 500}
    assert cash_flow["net_operating_cash_flow"] == 500
    assert cash_flow["investing_activities"] == {"1500 Office Equipment": -100}
    assert cash_flow["net_investing_cash_flow"] == -100
    assert cash_flow["financing_activities"] == {"2100 Bank Loan": 200}
    assert cash_flow["net_financing_cash_flow"] == 200


def test_gather_report_section_data_groups_imported_gl_lines_by_transaction_group_key(
    monkeypatch,
) -> None:
    """Imported GL cash flow should use persisted grouping keys instead of heuristics."""

    _BUDGET_FIXTURES.clear()
    session = _build_session()
    entity_id = uuid4()
    close_run_id = uuid4()
    batch_id = uuid4()
    _seed_close_run(
        session,
        close_run_id=close_run_id,
        entity_id=entity_id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    session.add(
        GeneralLedgerImportBatch(
            id=batch_id,
            entity_id=entity_id,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
            source_format="csv",
            uploaded_filename="march-gl.csv",
            row_count=6,
            imported_by_user_id=None,
            import_metadata={},
        )
    )
    session.add_all(
        (
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=1,
                posting_date=date(2026, 3, 5),
                account_code="1000",
                account_name="Bank Account",
                reference="GL-001",
                description="Customer receipt cash line",
                debit_amount="500.00",
                credit_amount="0.00",
                dimensions={},
                external_ref="EXT-001",
                transaction_group_key="glgrp_sale",
            ),
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=2,
                posting_date=date(2026, 3, 5),
                account_code="4000",
                account_name="Sales Revenue",
                reference="GL-001",
                description="Customer receipt revenue line",
                debit_amount="0.00",
                credit_amount="500.00",
                dimensions={},
                external_ref="EXT-001",
                transaction_group_key="glgrp_sale",
            ),
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=3,
                posting_date=date(2026, 3, 8),
                account_code="1500",
                account_name="Office Equipment",
                reference="GL-002",
                description="Equipment asset line",
                debit_amount="100.00",
                credit_amount="0.00",
                dimensions={},
                external_ref="EXT-002",
                transaction_group_key="glgrp_equipment",
            ),
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=4,
                posting_date=date(2026, 3, 8),
                account_code="1000",
                account_name="Bank Account",
                reference="GL-002",
                description="Equipment cash line",
                debit_amount="0.00",
                credit_amount="100.00",
                dimensions={},
                external_ref="EXT-002",
                transaction_group_key="glgrp_equipment",
            ),
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=5,
                posting_date=date(2026, 3, 12),
                account_code="1000",
                account_name="Bank Account",
                reference="GL-003",
                description="Loan drawdown cash line",
                debit_amount="200.00",
                credit_amount="0.00",
                dimensions={},
                external_ref="EXT-003",
                transaction_group_key="glgrp_loan",
            ),
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=batch_id,
                line_no=6,
                posting_date=date(2026, 3, 12),
                account_code="2100",
                account_name="Bank Loan",
                reference="GL-003",
                description="Loan drawdown liability line",
                debit_amount="0.00",
                credit_amount="200.00",
                dimensions={},
                external_ref="EXT-003",
                transaction_group_key="glgrp_loan",
            ),
        )
    )
    session.add(
        CloseRunLedgerBinding(
            id=uuid4(),
            close_run_id=close_run_id,
            general_ledger_import_batch_id=batch_id,
            trial_balance_import_batch_id=None,
            binding_source="auto",
            bound_by_user_id=None,
        )
    )
    session.commit()

    monkeypatch.setattr(
        section_data_module,
        "load_active_coa_accounts",
        lambda session, close_run_id: {
            "1000": {
                "account_code": "1000",
                "account_name": "Bank Account",
                "account_type": "asset",
                "is_postable": True,
            },
            "1500": {
                "account_code": "1500",
                "account_name": "Office Equipment",
                "account_type": "asset",
                "is_postable": True,
            },
            "2100": {
                "account_code": "2100",
                "account_name": "Bank Loan",
                "account_type": "liability",
                "is_postable": True,
            },
            "4000": {
                "account_code": "4000",
                "account_name": "Sales Revenue",
                "account_type": "revenue",
                "is_postable": True,
            },
        },
    )

    section_data = section_data_module.gather_report_section_data(
        session,
        close_run_id,
        ["cash_flow"],
    )
    cash_flow = section_data["cash_flow"]

    assert cash_flow["operating_activities"] == {"4000 Sales Revenue": 500}
    assert cash_flow["investing_activities"] == {"1500 Office Equipment": -100}
    assert cash_flow["financing_activities"] == {"2100 Bank Loan": 200}
