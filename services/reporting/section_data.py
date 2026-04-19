"""
Purpose: Build deterministic report-section data from canonical close-run sources.
Scope: Profit and loss, balance sheet, budget-variance, and KPI dashboard inputs
for report generation using effective-ledger activity, approved supporting schedules,
and imported trial-balance baselines where available.
Dependencies: Close-run models, ledger loaders, supporting-schedule persistence,
and canonical accounting enums only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from services.common.enums import (
    AccountType,
    ReportSectionKey,
    SupportingScheduleStatus,
    SupportingScheduleType,
)
from services.db.models.close_run import CloseRun
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ledger import (
    GeneralLedgerImportLine,
    TrialBalanceImportLine,
)
from services.db.repositories.supporting_schedule_repo import SupportingScheduleRepository
from services.ledger.effective_ledger import (
    load_active_coa_accounts,
    load_close_run_ledger_binding,
    load_effective_ledger_transactions,
)
from sqlalchemy import desc
from sqlalchemy.orm import Session

_CASH_ACCOUNT_TOKENS = ("bank", "cash", "petty cash")
_INVESTING_ACCOUNT_TOKENS = (
    "asset",
    "building",
    "computer",
    "equipment",
    "furniture",
    "intangible",
    "investment",
    "land",
    "leasehold",
    "machinery",
    "plant",
    "property",
    "software",
    "vehicle",
)
_FINANCING_ACCOUNT_TOKENS = (
    "bond",
    "borrow",
    "capital",
    "debt",
    "dividend",
    "draw",
    "equity",
    "lease liability",
    "loan",
    "mortgage",
    "note payable",
    "shareholder",
)


@dataclass(frozen=True, slots=True)
class _PreparedTransaction:
    """Carry one current-period transaction line in reporting-friendly form."""

    account_code: str
    account_name: str
    account_type: str
    posting_date: date
    period: str
    dimensions: dict[str, Any]
    presentation_amount: Decimal


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """Capture the deterministic reporting facts for one close run."""

    close_run_id: UUID
    entity_id: UUID
    period_start: date
    period_end: date
    account_lookup: dict[str, dict[str, Any]]
    period_transactions: tuple[_PreparedTransaction, ...]
    activity_by_account: dict[str, Decimal]
    balance_by_account: dict[str, Decimal]


def gather_report_section_data(
    session: Session,
    close_run_id: UUID,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """Return deterministic section data for the requested report sections."""

    close_run = session.get(CloseRun, close_run_id)
    if close_run is None:
        return {}

    requested = set(sections) if sections else {key.value for key in ReportSectionKey}
    current_snapshot = _build_run_snapshot(session=session, close_run=close_run)
    prior_close_run = (
        _load_previous_close_run(session=session, close_run=close_run)
        if (
            ReportSectionKey.CASH_FLOW.value in requested
            or ReportSectionKey.KPI_DASHBOARD.value in requested
        )
        else None
    )
    prior_snapshot = (
        _build_run_snapshot(session=session, close_run=prior_close_run)
        if prior_close_run is not None
        else None
    )

    data: dict[str, Any] = {}
    if ReportSectionKey.PROFIT_AND_LOSS.value in requested:
        data[ReportSectionKey.PROFIT_AND_LOSS.value] = _build_profit_and_loss_data(
            snapshot=current_snapshot
        )
    if ReportSectionKey.BALANCE_SHEET.value in requested:
        data[ReportSectionKey.BALANCE_SHEET.value] = _build_balance_sheet_data(
            snapshot=current_snapshot
        )
    if ReportSectionKey.CASH_FLOW.value in requested:
        data[ReportSectionKey.CASH_FLOW.value] = _build_cash_flow_data(
            session=session,
            close_run=close_run,
            snapshot=current_snapshot,
            prior_snapshot=prior_snapshot,
        )

    budget_variance_data: dict[str, Any] | None = None
    if (
        ReportSectionKey.BUDGET_VARIANCE.value in requested
        or ReportSectionKey.KPI_DASHBOARD.value in requested
    ):
        budget_variance_data = _build_budget_variance_data(
            session=session,
            snapshot=current_snapshot,
        )
    if ReportSectionKey.BUDGET_VARIANCE.value in requested:
        data[ReportSectionKey.BUDGET_VARIANCE.value] = budget_variance_data or {}
    if ReportSectionKey.KPI_DASHBOARD.value in requested:
        data[ReportSectionKey.KPI_DASHBOARD.value] = _build_kpi_dashboard_data(
            current_snapshot=current_snapshot,
            prior_snapshot=prior_snapshot,
            budget_variance_data=budget_variance_data or {},
        )

    return data


def load_budget_variance_data(session: Session, close_run_id: UUID) -> dict[str, Any]:
    """Return deterministic budget-vs-actual data for one close run."""

    close_run = session.get(CloseRun, close_run_id)
    if close_run is None:
        return {}
    return _build_budget_variance_data(
        session=session,
        snapshot=_build_run_snapshot(session=session, close_run=close_run),
    )


def load_kpi_dashboard_data(session: Session, close_run_id: UUID) -> dict[str, Any]:
    """Return deterministic KPI dashboard data for one close run."""

    close_run = session.get(CloseRun, close_run_id)
    if close_run is None:
        return {}
    current_snapshot = _build_run_snapshot(session=session, close_run=close_run)
    prior_close_run = _load_previous_close_run(session=session, close_run=close_run)
    prior_snapshot = (
        _build_run_snapshot(session=session, close_run=prior_close_run)
        if prior_close_run is not None
        else None
    )
    return _build_kpi_dashboard_data(
        current_snapshot=current_snapshot,
        prior_snapshot=prior_snapshot,
        budget_variance_data=_build_budget_variance_data(
            session=session,
            snapshot=current_snapshot,
        ),
    )


def _build_run_snapshot(*, session: Session, close_run: CloseRun) -> _RunSnapshot:
    """Prepare ledger and balance facts for one close run."""

    account_lookup = load_active_coa_accounts(session, close_run.id)
    period_transactions = _prepare_period_transactions(
        close_run=close_run,
        account_lookup=account_lookup,
        raw_transactions=load_effective_ledger_transactions(session, close_run.id),
    )
    activity_by_account: dict[str, Decimal] = {}
    for transaction in period_transactions:
        activity_by_account[transaction.account_code] = (
            activity_by_account.get(transaction.account_code, Decimal("0.00"))
            + transaction.presentation_amount
        )

    return _RunSnapshot(
        close_run_id=close_run.id,
        entity_id=close_run.entity_id,
        period_start=close_run.period_start,
        period_end=close_run.period_end,
        account_lookup=account_lookup,
        period_transactions=tuple(period_transactions),
        activity_by_account=activity_by_account,
        balance_by_account=_load_balance_by_account(
            session=session,
            close_run=close_run,
            account_lookup=account_lookup,
        ),
    )


def _prepare_period_transactions(
    *,
    close_run: CloseRun,
    account_lookup: dict[str, dict[str, Any]],
    raw_transactions: list[dict[str, Any]],
) -> list[_PreparedTransaction]:
    """Normalize raw effective-ledger rows into current-period reporting transactions."""

    prepared: list[_PreparedTransaction] = []
    for row in raw_transactions:
        posting_date = _coerce_date(row.get("date"))
        if posting_date is None:
            continue
        if posting_date < close_run.period_start or posting_date > close_run.period_end:
            continue
        account_code = str(row.get("account_code") or "").strip()
        if not account_code:
            continue
        account_info = account_lookup.get(account_code, {})
        account_type = str(account_info.get("account_type") or "")
        signed_amount = _coerce_decimal(row.get("signed_amount")) or Decimal("0.00")
        prepared.append(
            _PreparedTransaction(
                account_code=account_code,
                account_name=str(
                    row.get("account_name")
                    or account_info.get("account_name")
                    or account_code
                ),
                account_type=account_type,
                posting_date=posting_date,
                period=str(row.get("period") or posting_date.strftime("%Y-%m")),
                dimensions=(
                    dict(row.get("dimensions"))
                    if isinstance(row.get("dimensions"), dict)
                    else {}
                ),
                presentation_amount=_normalize_signed_amount(
                    signed_amount=signed_amount,
                    account_type=account_type,
                ),
            )
        )
    return prepared


def _build_profit_and_loss_data(*, snapshot: _RunSnapshot) -> dict[str, Any]:
    """Build the profit-and-loss section from current-period ledger activity."""

    revenue = _build_activity_bucket(
        snapshot=snapshot,
        allowed_account_types=(AccountType.REVENUE.value, AccountType.OTHER_INCOME.value),
        prefix_for_other={AccountType.OTHER_INCOME.value: "Other income"},
    )
    cost_of_sales = _build_activity_bucket(
        snapshot=snapshot,
        allowed_account_types=(AccountType.COST_OF_SALES.value,),
    )
    operating_expenses = _build_activity_bucket(
        snapshot=snapshot,
        allowed_account_types=(AccountType.EXPENSE.value, AccountType.OTHER_EXPENSE.value),
        prefix_for_other={AccountType.OTHER_EXPENSE.value: "Other expense"},
    )

    if not revenue and not cost_of_sales and not operating_expenses:
        return {}

    gross_profit = sum(revenue.values(), Decimal("0.00")) - sum(
        cost_of_sales.values(), Decimal("0.00")
    )
    net_profit = gross_profit - sum(operating_expenses.values(), Decimal("0.00"))
    return {
        "revenue": revenue,
        "cost_of_sales": cost_of_sales,
        "gross_profit": gross_profit,
        "operating_expenses": operating_expenses,
        "net_profit": net_profit,
    }


def _build_balance_sheet_data(*, snapshot: _RunSnapshot) -> dict[str, Any]:
    """Build the balance-sheet section when a TB baseline exists for the close run."""

    if not snapshot.balance_by_account:
        return {}

    assets = _build_balance_bucket(
        snapshot=snapshot,
        allowed_account_types=(AccountType.ASSET.value,),
    )
    liabilities = _build_balance_bucket(
        snapshot=snapshot,
        allowed_account_types=(AccountType.LIABILITY.value,),
    )
    equity = _build_balance_bucket(
        snapshot=snapshot,
        allowed_account_types=(AccountType.EQUITY.value,),
    )
    current_period_earnings = _calculate_current_period_earnings(snapshot=snapshot)
    if current_period_earnings != Decimal("0.00"):
        equity["Current period earnings"] = current_period_earnings

    if not assets and not liabilities and not equity:
        return {}

    return {
        "assets": assets,
        "total_assets": sum(assets.values(), Decimal("0.00")),
        "liabilities": liabilities,
        "total_liabilities": sum(liabilities.values(), Decimal("0.00")),
        "equity": equity,
        "total_equity": sum(equity.values(), Decimal("0.00")),
    }


def _build_cash_flow_data(
    *,
    session: Session,
    close_run: CloseRun,
    snapshot: _RunSnapshot,
    prior_snapshot: _RunSnapshot | None,
) -> dict[str, Any]:
    """Build a deterministic direct-method cash-flow section from cash movements."""

    cash_account_codes = _resolve_cash_account_codes(account_lookup=snapshot.account_lookup)
    if not cash_account_codes:
        return {}

    grouped_lines = _load_cash_flow_line_groups(
        session=session,
        close_run=close_run,
        account_lookup=snapshot.account_lookup,
    )
    operating: dict[str, Decimal] = {}
    investing: dict[str, Decimal] = {}
    financing: dict[str, Decimal] = {}
    for group in grouped_lines:
        _apply_cash_flow_group(
            lines=group,
            cash_account_codes=cash_account_codes,
            operating=operating,
            investing=investing,
            financing=financing,
        )

    if not operating and not investing and not financing:
        return {}

    net_operating = sum(operating.values(), Decimal("0.00"))
    net_investing = sum(investing.values(), Decimal("0.00"))
    net_financing = sum(financing.values(), Decimal("0.00"))
    net_change = net_operating + net_investing + net_financing
    closing_cash = _sum_cash_like_balances(snapshot=snapshot)
    opening_cash = (
        _sum_cash_like_balances(snapshot=prior_snapshot)
        if prior_snapshot is not None
        else (
            closing_cash - net_change
            if closing_cash is not None
            else None
        )
    )
    return {
        "operating_activities": dict(sorted(operating.items(), key=lambda item: item[0])),
        "net_operating_cash_flow": net_operating,
        "investing_activities": dict(sorted(investing.items(), key=lambda item: item[0])),
        "net_investing_cash_flow": net_investing,
        "financing_activities": dict(sorted(financing.items(), key=lambda item: item[0])),
        "net_financing_cash_flow": net_financing,
        "opening_cash_balance": opening_cash,
        "closing_cash_balance": closing_cash,
    }


def _build_budget_variance_data(*, session: Session, snapshot: _RunSnapshot) -> dict[str, Any]:
    """Build approved budget-vs-actual items for the current close run."""

    schedule_repo = SupportingScheduleRepository(session=session)
    schedule = schedule_repo.get_schedule(
        close_run_id=snapshot.close_run_id,
        schedule_type=SupportingScheduleType.BUDGET_VS_ACTUAL,
    )
    if schedule is None or schedule.status is not SupportingScheduleStatus.APPROVED:
        return {}

    row_records = schedule_repo.list_rows(schedule_id=schedule.id)
    valid_periods = _enumerate_period_labels(
        start=snapshot.period_start,
        end=snapshot.period_end,
    )
    items: list[dict[str, Any]] = []
    for row in row_records:
        payload = row.payload if isinstance(row.payload, dict) else {}
        budget_period = str(payload.get("period") or "").strip()
        if not budget_period or budget_period not in valid_periods:
            continue
        account_code = str(payload.get("account_code") or "").strip()
        if not account_code:
            continue
        budget_amount = _coerce_decimal(payload.get("budget_amount"))
        if budget_amount is None:
            continue
        actual_amount = sum(
            transaction.presentation_amount
            for transaction in snapshot.period_transactions
            if transaction.account_code == account_code
            and transaction.period == budget_period
            and _transaction_matches_budget_dimensions(
                transaction_dimensions=transaction.dimensions,
                budget_payload=payload,
            )
        )
        items.append(
            {
                "label": _build_budget_item_label(
                    account_code=account_code,
                    account_name=str(
                        snapshot.account_lookup.get(account_code, {}).get(
                            "account_name",
                            account_code,
                        )
                    ),
                    budget_payload=payload,
                ),
                "account_code": account_code,
                "period": budget_period,
                "budget": budget_amount,
                "actual": actual_amount,
                "variance": actual_amount - budget_amount,
                "department": payload.get("department"),
                "cost_centre": payload.get("cost_centre"),
                "project": payload.get("project"),
            }
        )

    if not items:
        return {}

    return {
        "items": sorted(
            items,
            key=lambda item: (
                str(item.get("period") or ""),
                str(item.get("account_code") or ""),
                str(item.get("label") or ""),
            ),
        ),
        "budget_row_count": len(items),
    }


def _build_kpi_dashboard_data(
    *,
    current_snapshot: _RunSnapshot,
    prior_snapshot: _RunSnapshot | None,
    budget_variance_data: dict[str, Any],
) -> dict[str, Any]:
    """Build the KPI dashboard from deterministic current and prior-period facts."""

    current_pl = _build_profit_and_loss_data(snapshot=current_snapshot)
    if not current_pl:
        return {}

    prior_pl = (
        _build_profit_and_loss_data(snapshot=prior_snapshot)
        if prior_snapshot is not None
        else {}
    )
    current_revenue = sum(current_pl.get("revenue", {}).values(), Decimal("0.00"))
    current_gross_profit = _coerce_decimal(current_pl.get("gross_profit")) or Decimal("0.00")
    current_net_profit = _coerce_decimal(current_pl.get("net_profit")) or Decimal("0.00")
    current_operating_expenses = sum(
        current_pl.get("operating_expenses", {}).values(),
        Decimal("0.00"),
    )
    prior_revenue = sum(prior_pl.get("revenue", {}).values(), Decimal("0.00"))
    prior_gross_profit = _coerce_decimal(prior_pl.get("gross_profit")) or Decimal("0.00")
    prior_net_profit = _coerce_decimal(prior_pl.get("net_profit")) or Decimal("0.00")
    prior_operating_expenses = sum(
        prior_pl.get("operating_expenses", {}).values(),
        Decimal("0.00"),
    )
    current_total_equity = _sum_balance_for_type(
        snapshot=current_snapshot,
        allowed_account_types=(AccountType.EQUITY.value,),
    ) + current_net_profit
    prior_total_equity = (
        _sum_balance_for_type(
            snapshot=prior_snapshot,
            allowed_account_types=(AccountType.EQUITY.value,),
        ) + prior_net_profit
        if prior_snapshot is not None
        else None
    )

    metrics: list[dict[str, Any]] = [
        {
            "label": "Revenue",
            "value": current_revenue,
            "prior_period": prior_revenue if prior_snapshot is not None else "N/A",
            "change": (
                current_revenue - prior_revenue
                if prior_snapshot is not None
                else "N/A"
            ),
        },
        {
            "label": "Gross Margin",
            "value": _format_percent_metric(
                _safe_percentage(current_gross_profit, current_revenue)
            ),
            "prior_period": (
                _format_percent_metric(
                    _safe_percentage(prior_gross_profit, prior_revenue)
                )
                if prior_snapshot is not None
                else "N/A"
            ),
            "change": (
                _format_percentage_point_delta(
                    _safe_percentage(current_gross_profit, current_revenue),
                    _safe_percentage(prior_gross_profit, prior_revenue),
                )
                if prior_snapshot is not None
                else "N/A"
            ),
        },
        {
            "label": "Net Margin",
            "value": _format_percent_metric(
                _safe_percentage(current_net_profit, current_revenue)
            ),
            "prior_period": (
                _format_percent_metric(
                    _safe_percentage(prior_net_profit, prior_revenue)
                )
                if prior_snapshot is not None
                else "N/A"
            ),
            "change": (
                _format_percentage_point_delta(
                    _safe_percentage(current_net_profit, current_revenue),
                    _safe_percentage(prior_net_profit, prior_revenue),
                )
                if prior_snapshot is not None
                else "N/A"
            ),
        },
        {
            "label": "Operating Expense Ratio",
            "value": _format_percent_metric(
                _safe_percentage(current_operating_expenses, current_revenue)
            ),
            "prior_period": (
                _format_percent_metric(
                    _safe_percentage(prior_operating_expenses, prior_revenue)
                )
                if prior_snapshot is not None
                else "N/A"
            ),
            "change": (
                _format_percentage_point_delta(
                    _safe_percentage(current_operating_expenses, current_revenue),
                    _safe_percentage(prior_operating_expenses, prior_revenue),
                )
                if prior_snapshot is not None
                else "N/A"
            ),
        },
    ]

    budget_items = tuple(
        item for item in budget_variance_data.get("items", ()) if isinstance(item, dict)
    )
    if budget_items:
        current_budget = sum(
            (_coerce_decimal(item.get("budget")) or Decimal("0.00")) for item in budget_items
        )
        current_actual = sum(
            (_coerce_decimal(item.get("actual")) or Decimal("0.00")) for item in budget_items
        )
        metrics.append(
            {
                "label": "Budget Attainment",
                "value": _format_percent_metric(
                    _safe_percentage(current_actual, current_budget)
                ),
                "prior_period": "N/A",
                "change": "N/A",
            }
        )

    current_total_liabilities = _sum_balance_for_type(
        snapshot=current_snapshot,
        allowed_account_types=(AccountType.LIABILITY.value,),
    )
    current_cash_balance = _sum_cash_like_balances(snapshot=current_snapshot)
    if current_total_equity > 0:
        prior_total_liabilities = (
            _sum_balance_for_type(
                snapshot=prior_snapshot,
                allowed_account_types=(AccountType.LIABILITY.value,),
            )
            if prior_snapshot is not None
            else None
        )
        current_debt_to_equity = current_total_liabilities / current_total_equity
        metrics.append(
            {
                "label": "Debt-to-Equity",
                "value": _format_ratio_metric(current_debt_to_equity),
                "prior_period": (
                    _format_ratio_metric(prior_total_liabilities / prior_total_equity)
                    if (
                        prior_snapshot is not None
                        and prior_total_liabilities is not None
                        and prior_total_equity is not None
                        and prior_total_equity > 0
                    )
                    else "N/A"
                ),
                "change": (
                    _format_ratio_delta(
                        current_debt_to_equity,
                        prior_total_liabilities / prior_total_equity,
                    )
                    if (
                        prior_snapshot is not None
                        and prior_total_liabilities is not None
                        and prior_total_equity is not None
                        and prior_total_equity > 0
                    )
                    else "N/A"
                ),
            }
        )
        metrics.append(
            {
                "label": "Return on Equity",
                "value": _format_percent_metric(
                    _safe_percentage(current_net_profit, current_total_equity)
                ),
                "prior_period": (
                    _format_percent_metric(
                        _safe_percentage(prior_net_profit, prior_total_equity)
                    )
                    if (
                        prior_snapshot is not None
                        and prior_total_equity is not None
                        and prior_total_equity > 0
                    )
                    else "N/A"
                ),
                "change": (
                    _format_percentage_point_delta(
                        _safe_percentage(current_net_profit, current_total_equity),
                        _safe_percentage(prior_net_profit, prior_total_equity),
                    )
                    if (
                        prior_snapshot is not None
                        and prior_total_equity is not None
                        and prior_total_equity > 0
                    )
                    else "N/A"
                ),
            }
        )

    if current_cash_balance is not None:
        prior_cash_balance = (
            _sum_cash_like_balances(snapshot=prior_snapshot)
            if prior_snapshot is not None
            else None
        )
        metrics.append(
            {
                "label": "Closing Cash Balance",
                "value": current_cash_balance,
                "prior_period": prior_cash_balance if prior_cash_balance is not None else "N/A",
                "change": (
                    current_cash_balance - prior_cash_balance
                    if prior_cash_balance is not None
                    else "N/A"
                ),
            }
        )

    return {"metrics": tuple(metrics)}


def _build_activity_bucket(
    *,
    snapshot: _RunSnapshot,
    allowed_account_types: tuple[str, ...],
    prefix_for_other: dict[str, str] | None = None,
) -> dict[str, Decimal]:
    """Aggregate current-period activity for the requested account families."""

    bucket: dict[str, Decimal] = {}
    for account_code, amount in snapshot.activity_by_account.items():
        account_info = snapshot.account_lookup.get(account_code, {})
        account_type = str(account_info.get("account_type") or "")
        if account_type not in allowed_account_types:
            continue
        if amount == Decimal("0.00"):
            continue
        account_name = str(account_info.get("account_name") or account_code)
        label = f"{account_code} {account_name}"
        if prefix_for_other and account_type in prefix_for_other:
            label = f"{prefix_for_other[account_type]} - {label}"
        bucket[label] = bucket.get(label, Decimal("0.00")) + amount
    return dict(sorted(bucket.items(), key=lambda item: item[0]))


def _build_balance_bucket(
    *,
    snapshot: _RunSnapshot,
    allowed_account_types: tuple[str, ...],
) -> dict[str, Decimal]:
    """Aggregate ending balances for the requested balance-sheet account families."""

    bucket: dict[str, Decimal] = {}
    for account_code, amount in snapshot.balance_by_account.items():
        account_info = snapshot.account_lookup.get(account_code, {})
        account_type = str(account_info.get("account_type") or "")
        if account_type not in allowed_account_types:
            continue
        if amount == Decimal("0.00"):
            continue
        account_name = str(account_info.get("account_name") or account_code)
        bucket[f"{account_code} {account_name}"] = amount
    return dict(sorted(bucket.items(), key=lambda item: item[0]))


def _load_balance_by_account(
    *,
    session: Session,
    close_run: CloseRun,
    account_lookup: dict[str, dict[str, Any]],
) -> dict[str, Decimal]:
    """Return ending balances when a trial-balance baseline is bound to the run."""

    binding = load_close_run_ledger_binding(session, close_run.id)
    if binding is None or binding.trial_balance_import_batch_id is None:
        return {}

    balances: dict[str, dict[str, Decimal | str]] = {}
    trial_balance_lines = (
        session.query(TrialBalanceImportLine)
        .filter(TrialBalanceImportLine.batch_id == binding.trial_balance_import_batch_id)
        .order_by(TrialBalanceImportLine.line_no.asc())
        .all()
    )
    for line in trial_balance_lines:
        account_type = str(
            line.account_type or account_lookup.get(line.account_code, {}).get("account_type") or ""
        )
        balances[line.account_code] = {
            "debit": _coerce_decimal(line.debit_balance) or Decimal("0.00"),
            "credit": _coerce_decimal(line.credit_balance) or Decimal("0.00"),
            "account_type": account_type,
        }

    journals = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run.id,
            JournalEntry.status.in_(("approved", "applied")),
        )
        .all()
    )
    for journal in journals:
        lines = (
            session.query(JournalLine)
            .filter(JournalLine.journal_entry_id == journal.id)
            .all()
        )
        for line in lines:
            bucket = balances.setdefault(
                line.account_code,
                {
                    "debit": Decimal("0.00"),
                    "credit": Decimal("0.00"),
                    "account_type": str(
                        account_lookup.get(line.account_code, {}).get("account_type") or ""
                    ),
                },
            )
            amount = _coerce_decimal(line.amount) or Decimal("0.00")
            if line.line_type == "debit":
                bucket["debit"] = _coerce_decimal(bucket["debit"]) + amount
            else:
                bucket["credit"] = _coerce_decimal(bucket["credit"]) + amount

    normalized: dict[str, Decimal] = {}
    for account_code, bucket in balances.items():
        normalized[account_code] = _normalize_balance(
            debit_amount=_coerce_decimal(bucket.get("debit")) or Decimal("0.00"),
            credit_amount=_coerce_decimal(bucket.get("credit")) or Decimal("0.00"),
            account_type=str(bucket.get("account_type") or ""),
        )
    return normalized


def _load_cash_flow_line_groups(
    *,
    session: Session,
    close_run: CloseRun,
    account_lookup: dict[str, dict[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], ...]:
    """Return current-period grouped line collections for cash-flow classification."""

    groups: list[tuple[dict[str, Any], ...]] = []
    binding = load_close_run_ledger_binding(session, close_run.id)
    if binding is not None and binding.general_ledger_import_batch_id is not None:
        imported_lines = (
            session.query(GeneralLedgerImportLine)
            .filter(
                GeneralLedgerImportLine.batch_id == binding.general_ledger_import_batch_id,
                GeneralLedgerImportLine.posting_date >= close_run.period_start,
                GeneralLedgerImportLine.posting_date <= close_run.period_end,
            )
            .order_by(
                GeneralLedgerImportLine.posting_date.asc(),
                GeneralLedgerImportLine.line_no.asc(),
            )
            .all()
        )
        grouped_imported: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for line in imported_lines:
            group_key = (line.posting_date.isoformat(), line.transaction_group_key)
            grouped_imported.setdefault(group_key, []).append(
                {
                    "account_code": line.account_code,
                    "account_name": line.account_name
                    or str(
                        account_lookup.get(line.account_code, {}).get(
                            "account_name",
                            line.account_code,
                        )
                    ),
                    "account_type": str(
                        account_lookup.get(line.account_code, {}).get("account_type") or ""
                    ),
                    "amount": (
                        _coerce_decimal(line.debit_amount)
                        or _coerce_decimal(line.credit_amount)
                        or Decimal("0.00")
                    ),
                    "signed_amount": (
                        (_coerce_decimal(line.debit_amount) or Decimal("0.00"))
                        - (_coerce_decimal(line.credit_amount) or Decimal("0.00"))
                    ),
                }
            )
        groups.extend(tuple(lines) for lines in grouped_imported.values())

    journals = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run.id,
            JournalEntry.status.in_(("approved", "applied")),
            JournalEntry.posting_date >= close_run.period_start,
            JournalEntry.posting_date <= close_run.period_end,
        )
        .order_by(JournalEntry.posting_date.asc(), JournalEntry.id.asc())
        .all()
    )
    for journal in journals:
        lines = (
            session.query(JournalLine)
            .filter(JournalLine.journal_entry_id == journal.id)
            .order_by(JournalLine.line_no.asc())
            .all()
        )
        groups.append(
            tuple(
                {
                    "account_code": line.account_code,
                    "account_name": str(
                        account_lookup.get(line.account_code, {}).get(
                            "account_name",
                            line.account_code,
                        )
                    ),
                    "account_type": str(
                        account_lookup.get(line.account_code, {}).get("account_type") or ""
                    ),
                    "amount": _coerce_decimal(line.amount) or Decimal("0.00"),
                    "signed_amount": (
                        _coerce_decimal(line.amount) or Decimal("0.00")
                    )
                    if line.line_type == "debit"
                    else Decimal("0.00") - (
                        _coerce_decimal(line.amount) or Decimal("0.00")
                    ),
                }
                for line in lines
            )
        )

    return tuple(group for group in groups if group)


def _apply_cash_flow_group(
    *,
    lines: tuple[dict[str, Any], ...],
    cash_account_codes: frozenset[str],
    operating: dict[str, Decimal],
    investing: dict[str, Decimal],
    financing: dict[str, Decimal],
) -> None:
    """Allocate one transaction group's cash movement to direct cash-flow buckets."""

    cash_lines = tuple(
        line for line in lines if str(line.get("account_code") or "") in cash_account_codes
    )
    non_cash_lines = tuple(
        line for line in lines if str(line.get("account_code") or "") not in cash_account_codes
    )
    if not cash_lines or not non_cash_lines:
        return

    cash_total = sum(
        (_coerce_decimal(line.get("signed_amount")) or Decimal("0.00"))
        for line in cash_lines
    )
    if cash_total == Decimal("0.00"):
        return

    weighted_lines = [
        line
        for line in non_cash_lines
        if (_coerce_decimal(line.get("amount")) or Decimal("0.00")) > Decimal("0.00")
    ]
    if not weighted_lines:
        return

    total_weight = sum(
        (_coerce_decimal(line.get("amount")) or Decimal("0.00")) for line in weighted_lines
    )
    if total_weight == Decimal("0.00"):
        return

    allocated_so_far = Decimal("0.00")
    for index, line in enumerate(weighted_lines):
        weight = _coerce_decimal(line.get("amount")) or Decimal("0.00")
        allocated_amount = (
            (cash_total * weight / total_weight).quantize(Decimal("0.01"))
            if index < len(weighted_lines) - 1
            else cash_total - allocated_so_far
        )
        allocated_so_far += allocated_amount
        label = (
            f"{str(line.get('account_code') or '').strip()} "
            f"{str(line.get('account_name') or '').strip()}"
        ).strip()
        category = _classify_cash_flow_category(
            account_type=str(line.get("account_type") or ""),
            account_name=str(line.get("account_name") or ""),
        )
        bucket = (
            operating
            if category == "operating"
            else investing
            if category == "investing"
            else financing
        )
        bucket[label] = bucket.get(label, Decimal("0.00")) + allocated_amount


def _resolve_cash_account_codes(*, account_lookup: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Return asset accounts that look like cash/bank ledgers."""

    return frozenset(
        account_code
        for account_code, account in account_lookup.items()
        if str(account.get("account_type") or "") == AccountType.ASSET.value
        and any(
            token in str(account.get("account_name") or "").lower()
            for token in _CASH_ACCOUNT_TOKENS
        )
    )


def _classify_cash_flow_category(*, account_type: str, account_name: str) -> str:
    """Return the direct-method cash-flow category for one offset account."""

    normalized_name = account_name.lower()
    if account_type == AccountType.EQUITY.value:
        return "financing"
    if account_type == AccountType.LIABILITY.value:
        if any(token in normalized_name for token in _FINANCING_ACCOUNT_TOKENS):
            return "financing"
        return "operating"
    if account_type == AccountType.ASSET.value:
        if any(token in normalized_name for token in _INVESTING_ACCOUNT_TOKENS):
            return "investing"
        return "operating"
    return "operating"


def _load_previous_close_run(*, session: Session, close_run: CloseRun) -> CloseRun | None:
    """Return the immediately previous close run for the same entity when available."""

    return (
        session.query(CloseRun)
        .filter(
            CloseRun.entity_id == close_run.entity_id,
            CloseRun.period_end < close_run.period_start,
        )
        .order_by(desc(CloseRun.period_end), desc(CloseRun.current_version_no))
        .first()
    )


def _transaction_matches_budget_dimensions(
    *,
    transaction_dimensions: dict[str, Any],
    budget_payload: dict[str, Any],
) -> bool:
    """Return whether one ledger line matches the schedule row dimensions exactly."""

    for field_name in ("department", "cost_centre", "project"):
        expected = budget_payload.get(field_name)
        if expected in {None, ""}:
            continue
        if str(transaction_dimensions.get(field_name) or "").strip() != str(expected).strip():
            return False
    return True


def _build_budget_item_label(
    *,
    account_code: str,
    account_name: str,
    budget_payload: dict[str, Any],
) -> str:
    """Return one stable budget item label including dimension qualifiers."""

    parts = [f"{account_code} {account_name}".strip()]
    for field_name, label in (
        ("department", "Department"),
        ("cost_centre", "Cost centre"),
        ("project", "Project"),
    ):
        value = str(budget_payload.get(field_name) or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    return " | ".join(parts)


def _sum_balance_for_type(
    *,
    snapshot: _RunSnapshot | None,
    allowed_account_types: tuple[str, ...],
) -> Decimal:
    """Return the total normalized ending balance for the requested account families."""

    if snapshot is None:
        return Decimal("0.00")
    return sum(
        amount
        for account_code, amount in snapshot.balance_by_account.items()
        if str(snapshot.account_lookup.get(account_code, {}).get("account_type") or "")
        in allowed_account_types
    )


def _calculate_current_period_earnings(*, snapshot: _RunSnapshot) -> Decimal:
    """Return current-period net profit for balance-sheet equity presentation."""

    profit_and_loss = _build_profit_and_loss_data(snapshot=snapshot)
    return _coerce_decimal(profit_and_loss.get("net_profit")) or Decimal("0.00")


def _sum_cash_like_balances(*, snapshot: _RunSnapshot | None) -> Decimal | None:
    """Return the total ending balance of cash-like asset accounts when available."""

    if snapshot is None or not snapshot.balance_by_account:
        return None
    total = Decimal("0.00")
    matched = False
    for account_code, amount in snapshot.balance_by_account.items():
        account_info = snapshot.account_lookup.get(account_code, {})
        account_type = str(account_info.get("account_type") or "")
        account_name = str(account_info.get("account_name") or account_code).lower()
        if account_type != AccountType.ASSET.value:
            continue
        if not any(token in account_name for token in _CASH_ACCOUNT_TOKENS):
            continue
        total += amount
        matched = True
    return total if matched else None


def _enumerate_period_labels(*, start: date, end: date) -> set[str]:
    """Return all YYYY-MM period labels covered by the close-run date span."""

    labels: set[str] = set()
    year = start.year
    month = start.month
    while (year, month) <= (end.year, end.month):
        labels.add(f"{year:04d}-{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return labels


def _normalize_signed_amount(*, signed_amount: Decimal, account_type: str) -> Decimal:
    """Return a presentation amount that is positive in the account's normal direction."""

    if account_type in {
        AccountType.LIABILITY.value,
        AccountType.EQUITY.value,
        AccountType.REVENUE.value,
        AccountType.OTHER_INCOME.value,
    }:
        return signed_amount * Decimal("-1.00")
    return signed_amount


def _normalize_balance(
    *,
    debit_amount: Decimal,
    credit_amount: Decimal,
    account_type: str,
) -> Decimal:
    """Return the ending balance presented in the account's normal direction."""

    if account_type in {
        AccountType.LIABILITY.value,
        AccountType.EQUITY.value,
        AccountType.REVENUE.value,
        AccountType.OTHER_INCOME.value,
    }:
        return credit_amount - debit_amount
    return debit_amount - credit_amount


def _safe_percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Return a percentage in 0-100 form, or zero when the denominator is zero."""

    if denominator == Decimal("0.00"):
        return Decimal("0.00")
    return (numerator / denominator) * Decimal("100.00")


def _format_percent_metric(value: Decimal) -> str:
    """Render one percentage metric with a stable one-decimal suffix."""

    return f"{value.quantize(Decimal('0.1'))}%"


def _format_percentage_point_delta(current_value: Decimal, prior_value: Decimal) -> str:
    """Render the delta between two percentage metrics as percentage points."""

    delta = current_value - prior_value
    return f"{delta.quantize(Decimal('0.1')):+} pp"


def _format_ratio_metric(value: Decimal) -> str:
    """Render one ratio metric with two decimals and an x suffix."""

    return f"{value.quantize(Decimal('0.01'))}x"


def _format_ratio_delta(current_value: Decimal, prior_value: Decimal) -> str:
    """Render the delta between two ratio metrics."""

    delta = current_value - prior_value
    return f"{delta.quantize(Decimal('0.01')):+}x"


def _coerce_decimal(value: Any) -> Decimal | None:
    """Convert one JSON-like numeric value into a Decimal safely."""

    if isinstance(value, Decimal):
        return value
    if value in {None, ""}:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    """Convert one date-like value into a date when possible."""

    if isinstance(value, date):
        return value
    if value in {None, ""}:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


__all__ = [
    "gather_report_section_data",
    "load_budget_variance_data",
    "load_kpi_dashboard_data",
]
