"""
Purpose: Celery task that executes reconciliation matching for a close run.
Scope: Dispatches bank reconciliation, AR/AP ageing, intercompany, payroll control,
       fixed assets, loan amortisation, accrual tracker, budget vs actual, and trial
       balance reconciliation through the matching engine. Persists results and emits
       audit events.
Dependencies: Celery worker app, reconciliation service, matchers, DB session factory,
       audit service, and structured logging.

Design notes:
- This is the canonical entry point for all reconciliation execution.
- The task loads source data from the database (bank statements, ledger transactions,
  COA balances, etc.), dispatches to the appropriate matchers, and persists results.
- If source data is unavailable for a reconciliation type, that type is skipped with
  an explicit log message — NOT a silent fallback.
- Trial balance computation runs after all matching completes to validate overall integrity.
- The task records checkpoints so interrupted runs can resume from the last completed type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.worker.app.celery_app import celery_app
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from apps.worker.app.tasks.close_run_phase_guard import ensure_close_run_active_phase
from services.common.enums import ReconciliationType, WorkflowPhase
from services.common.logging import get_logger
from services.db.models.documents import Document, DocumentType
from services.db.models.extractions import DocumentExtraction
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ledger import (
    GeneralLedgerImportLine,
    TrialBalanceImportLine,
)
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.supporting_schedule_repo import SupportingScheduleRepository
from services.db.session import get_session_factory
from services.jobs.retry_policy import JobCancellationRequestedError
from services.jobs.task_names import TaskName, resolve_task_route
from services.ledger.effective_ledger import (
    load_active_coa_accounts as _load_coa_accounts,
)
from services.ledger.effective_ledger import (
    load_close_run_ledger_binding as _load_close_run_ledger_binding,
)
from services.ledger.effective_ledger import (
    load_effective_ledger_transactions as _load_ledger_transactions,
)
from services.reconciliation.applicability import (
    filter_runnable_reconciliation_types,
)
from services.reconciliation.matchers import DEFAULT_MATCHING_CONFIG, MatchingConfig
from services.reconciliation.service import ReconciliationRunOutput, ReconciliationService
from services.supporting_schedules.service import SupportingScheduleService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    """Describe the outcome of one reconciliation execution.

    Attributes:
        close_run_id: UUID of the close run reconciled.
        reconciliation_types: Types that were executed.
        total_items: Total reconciliation items created.
        matched_items: Items matched successfully.
        exception_items: Items requiring reviewer disposition.
        unmatched_items: Items with no counterpart found.
        trial_balance_computed: Whether trial balance was computed.
        trial_balance_balanced: Whether trial balance debits equal credits.
        errors: Explicit error messages encountered during execution.
    """

    close_run_id: str
    reconciliation_types: list[str]
    total_items: int
    matched_items: int
    exception_items: int
    unmatched_items: int
    trial_balance_computed: bool
    trial_balance_balanced: bool
    errors: list[str]


def _load_bank_statement_data(session, close_run_id: UUID) -> dict[str, list[dict[str, Any]]]:
    """Load bank statement lines from extracted bank statement documents.

    Args:
        session: Active SQLAlchemy session.
        close_run_id: The close run to load data for.

    Returns:
        Dict with 'source_items' and 'counterparts' keys.
    """
    source_items: list[dict[str, Any]] = []
    counterpart_index: dict[str, dict[str, Any]] = {}

    # Load bank statement documents
    bank_docs = (
        session.query(Document)
        .filter(
            Document.close_run_id == close_run_id,
            Document.document_type == DocumentType.BANK_STATEMENT.value,
        )
        .all()
    )

    for doc in bank_docs:
        latest_extraction = (
            session.query(DocumentExtraction)
            .filter(DocumentExtraction.document_id == doc.id)
            .order_by(
                DocumentExtraction.version_no.desc(),
                DocumentExtraction.created_at.desc(),
            )
            .first()
        )
        if latest_extraction is None:
            continue

        payload = latest_extraction.extracted_payload
        for line_data in _read_statement_lines_from_payload(payload=payload):
            line_ref = f"bank:{doc.id}:{line_data.get('line_no', 0)}"
            amount = _resolve_statement_line_amount(line_data)
            source_items.append(
                {
                    "ref": line_ref,
                    "amount": amount,
                    "date": line_data.get("date"),
                    "reference": line_data.get("reference", ""),
                    "description": line_data.get("description", ""),
                }
            )
            counterpart_index[line_ref] = {
                "source_type": "bank_statement_line",
                "document_id": str(doc.id),
            }

    return {
        "source_items": source_items,
        "counterparts": [],  # Counterparts are ledger transactions, loaded separately
        "counterpart_index": counterpart_index,
    }


def _resolve_statement_line_amount(line_data: dict[str, Any]) -> str | None:
    """Return the effective absolute amount for one bank-statement line."""

    explicit_amount = _parse_decimal_value(line_data.get("amount"))
    if explicit_amount is not None:
        return _decimal_to_string(explicit_amount)

    debit_amount = _parse_decimal_value(line_data.get("debit"))
    credit_amount = _parse_decimal_value(line_data.get("credit"))

    if debit_amount is not None and debit_amount > 0:
        return _decimal_to_string(debit_amount)
    if credit_amount is not None and credit_amount > 0:
        return _decimal_to_string(credit_amount)
    if debit_amount is not None:
        return _decimal_to_string(debit_amount)
    if credit_amount is not None:
        return _decimal_to_string(credit_amount)
    return None


def _parse_decimal_value(value: Any) -> Decimal | None:
    """Parse one statement value into a Decimal when possible."""

    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _read_statement_lines_from_payload(*, payload: Any) -> tuple[dict[str, Any], ...]:
    """Read bank-statement lines from either the legacy or normalized extraction payload."""

    if not isinstance(payload, dict):
        return ()

    parser_output = payload.get("parser_output")
    for candidate in (
        payload.get("statement_lines"),
        payload.get("lines"),
        (parser_output or {}).get("statement_lines") if isinstance(parser_output, dict) else None,
        (parser_output or {}).get("lines") if isinstance(parser_output, dict) else None,
    ):
        if isinstance(candidate, list):
            return tuple(item for item in candidate if isinstance(item, dict))

    return ()


def _compute_account_balances(session, close_run_id: UUID) -> list[dict[str, Any]]:
    """Compute effective account balances for the close run trial balance.

    Args:
        session: Active SQLAlchemy session.
        close_run_id: The close run to compute balances for.

    Returns:
        List of account balance dicts suitable for trial balance checking.
    """
    # Load COA accounts
    coa_accounts = _load_coa_accounts(session, close_run_id)

    balances: dict[str, dict[str, Any]] = {}
    binding = _load_close_run_ledger_binding(session, close_run_id)
    if binding is not None and binding.trial_balance_import_batch_id is not None:
        _seed_balances_from_imported_trial_balance(
            session=session,
            trial_balance_import_batch_id=binding.trial_balance_import_batch_id,
            coa_accounts=coa_accounts,
            balances=balances,
        )
        _apply_close_run_journal_deltas(
            session=session,
            close_run_id=close_run_id,
            coa_accounts=coa_accounts,
            balances=balances,
        )
        return list(balances.values())

    if binding is not None and binding.general_ledger_import_batch_id is not None:
        _seed_balances_from_imported_general_ledger(
            session=session,
            general_ledger_import_batch_id=binding.general_ledger_import_batch_id,
            coa_accounts=coa_accounts,
            balances=balances,
        )

    _apply_close_run_journal_deltas(
        session=session,
        close_run_id=close_run_id,
        coa_accounts=coa_accounts,
        balances=balances,
    )

    return list(balances.values())


def _seed_balances_from_imported_trial_balance(
    *,
    session,
    trial_balance_import_batch_id: UUID,
    coa_accounts: dict[str, dict[str, Any]],
    balances: dict[str, dict[str, Any]],
) -> None:
    """Seed account balances from one imported trial-balance batch."""

    lines = (
        session.query(TrialBalanceImportLine)
        .filter(TrialBalanceImportLine.batch_id == trial_balance_import_batch_id)
        .order_by(TrialBalanceImportLine.line_no)
        .all()
    )
    for line in lines:
        acct_info = coa_accounts.get(line.account_code, {})
        balances[line.account_code] = {
            "account_code": line.account_code,
            "account_name": line.account_name or acct_info.get("account_name", line.account_code),
            "account_type": line.account_type or acct_info.get("account_type", "unknown"),
            "debit_balance": Decimal(str(line.debit_balance)),
            "credit_balance": Decimal(str(line.credit_balance)),
            "is_active": bool(line.is_active),
        }


def _seed_balances_from_imported_general_ledger(
    *,
    session,
    general_ledger_import_batch_id: UUID,
    coa_accounts: dict[str, dict[str, Any]],
    balances: dict[str, dict[str, Any]],
) -> None:
    """Seed account balances by aggregating one imported general-ledger batch."""

    lines = (
        session.query(GeneralLedgerImportLine)
        .filter(GeneralLedgerImportLine.batch_id == general_ledger_import_batch_id)
        .order_by(GeneralLedgerImportLine.posting_date, GeneralLedgerImportLine.line_no)
        .all()
    )
    for line in lines:
        bucket = _ensure_balance_bucket(
            balances=balances,
            coa_accounts=coa_accounts,
            account_code=line.account_code,
            account_name=line.account_name,
            account_type=None,
        )
        bucket["debit_balance"] += Decimal(str(line.debit_amount))
        bucket["credit_balance"] += Decimal(str(line.credit_amount))


def _apply_close_run_journal_deltas(
    *,
    session,
    close_run_id: UUID,
    coa_accounts: dict[str, dict[str, Any]],
    balances: dict[str, dict[str, Any]],
) -> None:
    """Layer approved/applied close-run journal lines onto the running account balances."""

    journals = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run_id,
            JournalEntry.status.in_(["approved", "applied"]),
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
            bucket = _ensure_balance_bucket(
                balances=balances,
                coa_accounts=coa_accounts,
                account_code=line.account_code,
                account_name=None,
                account_type=None,
            )
            amount = Decimal(str(line.amount))
            if line.line_type == "debit":
                bucket["debit_balance"] += amount
            else:
                bucket["credit_balance"] += amount


def _ensure_balance_bucket(
    *,
    balances: dict[str, dict[str, Any]],
    coa_accounts: dict[str, dict[str, Any]],
    account_code: str,
    account_name: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    """Return a mutable balance bucket for one account code, creating it when needed."""

    if account_code not in balances:
        acct_info = coa_accounts.get(account_code, {})
        balances[account_code] = {
            "account_code": account_code,
            "account_name": account_name or acct_info.get("account_name", account_code),
            "account_type": account_type or acct_info.get("account_type", "unknown"),
            "debit_balance": Decimal("0.00"),
            "credit_balance": Decimal("0.00"),
            "is_active": True,
        }
    return balances[account_code]


def _build_reconciliation_source_data(
    session,
    close_run_id: UUID,
    reconciliation_types: list[ReconciliationType],
) -> dict[ReconciliationType, dict[str, list[dict[str, Any]]]]:
    """Build source data for all requested reconciliation types.

    Args:
        session: Active SQLAlchemy session.
        close_run_id: The close run to load data for.
        reconciliation_types: Types to prepare data for.

    Returns:
        Dict mapping reconciliation types to source/counterpart data.
    """
    source_data: dict[ReconciliationType, dict[str, list[dict[str, Any]]]] = {}
    ledger_transactions = _load_ledger_transactions(session, close_run_id)
    supporting_schedule_service = SupportingScheduleService(
        repository=SupportingScheduleRepository(session=session),
    )
    supporting_schedule_workspace = supporting_schedule_service.list_workspace(
        close_run_id=close_run_id
    )
    schedule_rows_by_type = {
        snapshot.schedule.schedule_type.value: [dict(row.payload) for row in snapshot.rows]
        for snapshot in supporting_schedule_workspace
        if snapshot.schedule.status.value != "not_applicable"
    }

    for rec_type in reconciliation_types:
        if rec_type == ReconciliationType.BANK_RECONCILIATION:
            bank_data = _load_bank_statement_data(session, close_run_id)
            source_data[rec_type] = {
                "source_items": bank_data["source_items"],
                "counterparts": ledger_transactions,
            }

        elif rec_type in (ReconciliationType.AR_AGEING, ReconciliationType.AP_AGEING):
            # For ageing, source items are extracted receivables/payables from invoices
            source_data[rec_type] = {
                "source_items": [],  # Populated from invoice extractions
                "counterparts": ledger_transactions,
            }

        elif rec_type == ReconciliationType.TRIAL_BALANCE:
            # Trial balance uses computed account balances
            account_balances = _compute_account_balances(session, close_run_id)
            source_data[rec_type] = {
                "source_items": account_balances,
                "counterparts": [],
            }

        elif rec_type == ReconciliationType.FIXED_ASSETS:
            source_items = schedule_rows_by_type.get(ReconciliationType.FIXED_ASSETS.value, [])
            source_data[rec_type] = {
                "source_items": source_items,
                "counterparts": _build_fixed_asset_counterparts(
                    source_items=source_items,
                    ledger_transactions=ledger_transactions,
                ),
            }

        elif rec_type == ReconciliationType.LOAN_AMORTISATION:
            source_items = schedule_rows_by_type.get(
                ReconciliationType.LOAN_AMORTISATION.value,
                [],
            )
            source_data[rec_type] = {
                "source_items": source_items,
                "counterparts": _build_loan_counterparts(
                    source_items=source_items,
                    ledger_transactions=ledger_transactions,
                ),
            }

        elif rec_type == ReconciliationType.ACCRUAL_TRACKER:
            source_items = schedule_rows_by_type.get(
                ReconciliationType.ACCRUAL_TRACKER.value,
                [],
            )
            source_data[rec_type] = {
                "source_items": source_items,
                "counterparts": _build_accrual_counterparts(
                    source_items=source_items,
                    ledger_transactions=ledger_transactions,
                ),
            }

        elif rec_type == ReconciliationType.BUDGET_VS_ACTUAL:
            source_items = schedule_rows_by_type.get(
                ReconciliationType.BUDGET_VS_ACTUAL.value,
                [],
            )
            source_data[rec_type] = {
                "source_items": source_items,
                "counterparts": _build_budget_counterparts(
                    source_items=source_items,
                    ledger_transactions=ledger_transactions,
                ),
            }

        else:
            # Other types: use ledger transactions as counterparts
            source_data[rec_type] = {
                "source_items": [],
                "counterparts": ledger_transactions,
            }

    return source_data


def _build_fixed_asset_counterparts(
    *,
    source_items: list[dict[str, Any]],
    ledger_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build fixed-asset ledger counterparts from tagged journal lines."""

    counterparts: list[dict[str, Any]] = []
    for item in source_items:
        asset_id = str(item.get("asset_id", "")).strip()
        if not asset_id:
            continue
        asset_lines = _filter_lines_by_tag(
            ledger_transactions,
            tags={asset_id},
            account_codes={
                str(item.get("asset_account_code", "")).strip(),
                str(item.get("accumulated_depreciation_account_code", "")).strip(),
            },
        )
        cost_total = _sum_signed_amounts(
            asset_lines,
            account_codes={str(item.get("asset_account_code", "")).strip()},
        )
        depreciation_total = _sum_signed_amounts(
            asset_lines,
            account_codes={str(item.get("accumulated_depreciation_account_code", "")).strip()},
        )
        counterparts.append(
            {
                "asset_id": asset_id,
                "ref": f"ledger:asset:{asset_id}",
                "cost": _decimal_abs_to_string(cost_total),
                "accumulated_depreciation": _decimal_abs_to_string(depreciation_total),
            }
        )
    return counterparts


def _build_loan_counterparts(
    *,
    source_items: list[dict[str, Any]],
    ledger_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build loan-amortisation counterparts from tagged journal lines."""

    counterparts: list[dict[str, Any]] = []
    for item in source_items:
        loan_id = str(item.get("loan_id", "")).strip()
        payment_no = str(item.get("payment_no", "")).strip()
        if not loan_id or not payment_no:
            continue
        tags = {loan_id, payment_no}
        due_date = _parse_iso_date(item.get("due_date"))
        payment_lines = _filter_lines_by_tag(
            ledger_transactions,
            tags=tags,
            account_codes={
                str(item.get("loan_account_code", "")).strip(),
                str(item.get("interest_account_code", "")).strip(),
            },
        )
        loan_history_lines = _filter_lines_by_tag(
            ledger_transactions,
            tags={loan_id},
            account_codes={str(item.get("loan_account_code", "")).strip()},
            up_to_date=due_date,
        )
        counterparts.append(
            {
                "payment_no": item.get("payment_no"),
                "ref": f"ledger:loan:{loan_id}:payment:{payment_no}",
                "principal": _decimal_abs_to_string(
                    _sum_signed_amounts(
                        payment_lines,
                        account_codes={str(item.get("loan_account_code", "")).strip()},
                    )
                ),
                "interest": _decimal_abs_to_string(
                    _sum_signed_amounts(
                        payment_lines,
                        account_codes={str(item.get("interest_account_code", "")).strip()},
                    )
                ),
                "balance": _decimal_abs_to_string(_sum_signed_amounts(loan_history_lines)),
            }
        )
    return counterparts


def _build_accrual_counterparts(
    *,
    source_items: list[dict[str, Any]],
    ledger_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build accrual-tracker counterparts from tagged journal lines."""

    counterparts: list[dict[str, Any]] = []
    for item in source_items:
        account_code = str(item.get("account_code", "")).strip()
        period = str(item.get("period", "")).strip()
        reference = str(item.get("ref", "")).strip()
        if not account_code or not period:
            continue
        lines = [
            line
            for line in ledger_transactions
            if str(line.get("account_code", "")).strip() == account_code
            and str(line.get("period", "")).strip() == period
            and (
                not reference
                or _line_contains_any_tag(line, {reference})
            )
        ]
        if not lines:
            continue
        counterparts.append(
            {
                "ref": f"ledger:accrual:{reference or account_code}:{period}",
                "account_code": account_code,
                "period": period,
                "amount": _decimal_abs_to_string(_sum_signed_amounts(lines)),
            }
        )
    return counterparts


def _build_budget_counterparts(
    *,
    source_items: list[dict[str, Any]],
    ledger_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build budget-vs-actual counterparts from ledger actuals."""

    counterparts: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str, str]] = set()
    for item in source_items:
        account_code = str(item.get("account_code", "")).strip()
        period = str(item.get("period", "")).strip()
        department = str(item.get("department", "")).strip()
        cost_centre = str(item.get("cost_centre", "")).strip()
        project = str(item.get("project", "")).strip()
        key = (account_code, period, department, cost_centre, project)
        if not account_code or not period or key in seen_keys:
            continue
        seen_keys.add(key)
        lines = [
            line
            for line in ledger_transactions
            if str(line.get("account_code", "")).strip() == account_code
            and str(line.get("period", "")).strip() == period
            and _dimensions_match(
                line.get("dimensions", {}),
                department=department or None,
                cost_centre=cost_centre or None,
                project=project or None,
            )
        ]
        if not lines:
            continue
        counterparts.append(
            {
                "ref": _build_budget_reference(
                    prefix="ledger:budget",
                    account_code=account_code,
                    period=period,
                    department=department,
                    cost_centre=cost_centre,
                    project=project,
                ),
                "account_code": account_code,
                "period": period,
                **({"department": department} if department else {}),
                **({"cost_centre": cost_centre} if cost_centre else {}),
                **({"project": project} if project else {}),
                "amount": _decimal_to_string(_sum_signed_amounts(lines)),
            }
        )
    return counterparts


def _build_budget_reference(
    *,
    prefix: str,
    account_code: str,
    period: str,
    department: str,
    cost_centre: str,
    project: str,
) -> str:
    """Build a stable budget reference that retains dimensional context."""

    reference_parts = [prefix, account_code, period]
    reference_parts.extend(
        dimension
        for dimension in (department, cost_centre, project)
        if dimension
    )
    return ":".join(reference_parts)


def _filter_lines_by_tag(
    ledger_transactions: list[dict[str, Any]],
    *,
    tags: set[str],
    account_codes: set[str],
    up_to_date: date | None = None,
) -> list[dict[str, Any]]:
    """Filter journal lines by account code plus a stable tag in ref or dimensions."""

    normalized_account_codes = {code for code in account_codes if code}
    filtered_lines: list[dict[str, Any]] = []
    for line in ledger_transactions:
        if (
            normalized_account_codes
            and str(line.get("account_code", "")).strip() not in normalized_account_codes
        ):
            continue
        if not _line_contains_any_tag(line, tags):
            continue
        parsed_date = _parse_iso_date(line.get("date"))
        if up_to_date is not None and (parsed_date is None or parsed_date > up_to_date):
            continue
        filtered_lines.append(line)
    return filtered_lines


def _line_contains_any_tag(line: dict[str, Any], tags: set[str]) -> bool:
    """Return whether a journal line carries any of the supplied tags."""

    normalized_tags = {tag for tag in (str(tag).strip() for tag in tags) if tag}
    if not normalized_tags:
        return False

    reference = str(line.get("reference", "")).strip()
    if reference and reference in normalized_tags:
        return True

    dimensions = line.get("dimensions", {})
    if isinstance(dimensions, dict):
        for value in dimensions.values():
            if str(value).strip() in normalized_tags:
                return True

    return False


def _dimensions_match(
    dimensions: Any,
    *,
    department: str | None,
    cost_centre: str | None,
    project: str | None,
) -> bool:
    """Return whether a line matches the supplied optional budget dimensions."""

    if not isinstance(dimensions, dict):
        dimensions = {}
    if department and str(dimensions.get("department", "")).strip() != department:
        return False
    if cost_centre and str(dimensions.get("cost_centre", "")).strip() != cost_centre:
        return False
    if project and str(dimensions.get("project", "")).strip() != project:
        return False
    return True


def _sum_signed_amounts(
    lines: list[dict[str, Any]],
    *,
    account_codes: set[str] | None = None,
) -> Decimal:
    """Return the signed journal-line amount total for the supplied filtered lines."""

    normalized_account_codes = {code for code in (account_codes or set()) if code}
    total = Decimal("0.00")
    for line in lines:
        if (
            normalized_account_codes
            and str(line.get("account_code", "")).strip() not in normalized_account_codes
        ):
            continue
        total += Decimal(str(line.get("signed_amount", "0.00")))
    return total.quantize(Decimal("0.01"))


def _decimal_abs_to_string(value: Decimal) -> str:
    """Return a positive decimal string for a ledger aggregate."""

    return _decimal_to_string(abs(value))


def _decimal_to_string(value: Decimal) -> str:
    """Return a normalized decimal string for reconciliation payloads."""

    return f"{value.quantize(Decimal('0.01')):.2f}"


def _parse_iso_date(value: Any) -> date | None:
    """Parse an ISO date string when available."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _run_reconciliation_task(
    *,
    close_run_id: str,
    reconciliation_types: list[str],
    actor_user_id: str | None = None,
    matching_config: dict[str, Any] | None = None,
    job_context: JobRuntimeContext,
) -> dict[str, Any]:
    """Execute reconciliation matching for a close run.

    This Celery task is the canonical entry point for reconciliation execution.
    It loads source data from the database, dispatches to the appropriate matchers,
    persists results, computes trial balance, and records anomalies.

    Args:
        close_run_id: UUID of the close run to reconcile.
        reconciliation_types: List of reconciliation type values to execute.
        actor_user_id: Optional UUID of the user triggering the run.
        matching_config: Optional matching configuration overrides.

    Returns:
        Dict with reconciliation receipt data.
    """
    parsed_close_run_id = UUID(close_run_id)
    parsed_actor_user_id = UUID(actor_user_id) if actor_user_id else None
    parsed_types = [ReconciliationType(t) for t in reconciliation_types]

    config = MatchingConfig(**matching_config) if matching_config else DEFAULT_MATCHING_CONFIG
    logger.info(
        "Starting reconciliation for close run %s, types=%s",
        close_run_id,
        reconciliation_types,
    )

    with get_session_factory()() as session:
        repo = ReconciliationRepository(session)
        svc = ReconciliationService(repository=repo, matching_config=config)

        def ensure_reconciliation_phase() -> None:
            job_context.ensure_not_canceled()
            ensure_close_run_active_phase(
                session=session,
                close_run_id=parsed_close_run_id,
                required_phase=WorkflowPhase.RECONCILIATION,
            )

        # Build source data
        try:
            ensure_reconciliation_phase()
            source_data = _build_reconciliation_source_data(
                session, parsed_close_run_id, parsed_types
            )
            job_context.checkpoint(
                step="load_reconciliation_sources",
                state={
                    "close_run_id": close_run_id,
                    "reconciliation_types": reconciliation_types,
                },
            )
            ensure_reconciliation_phase()
        except JobCancellationRequestedError:
            session.rollback()
            raise
        except Exception as exc:
            msg = f"Failed to load reconciliation source data: {exc}"
            logger.exception(msg)
            session.rollback()
            raise RuntimeError(msg) from exc

        runnable_types, skipped_guidance = filter_runnable_reconciliation_types(
            requested_types=parsed_types,
            source_data=source_data,
        )
        if skipped_guidance:
            logger.info(
                "Skipped non-applicable reconciliation types for close run %s.",
                close_run_id,
                guidance=list(skipped_guidance),
            )
        matching_types = tuple(
            reconciliation_type
            for reconciliation_type in runnable_types
            if reconciliation_type is not ReconciliationType.TRIAL_BALANCE
        )
        trial_balance_requested = ReconciliationType.TRIAL_BALANCE in runnable_types
        if not matching_types and not trial_balance_requested:
            session.rollback()
            raise RuntimeError(
                "No runnable reconciliation work was available for the requested types."
            )

        # Run reconciliation matching
        try:
            if matching_types:
                output = svc.run_reconciliation(
                    close_run_id=parsed_close_run_id,
                    reconciliation_types=list(matching_types),
                    source_data=source_data,
                    created_by_user_id=parsed_actor_user_id,
                    matching_config=config,
                    progress_guard=ensure_reconciliation_phase,
                )
            else:
                output = ReconciliationRunOutput(
                    reconciliations=[],
                    all_items=[],
                    trial_balance=None,
                    anomalies=[],
                    total_items=0,
                    matched_items=0,
                    exception_items=0,
                    unmatched_items=0,
                )
            job_context.checkpoint(
                step="run_reconciliation_matching",
                state={
                    "total_items": output.total_items,
                    "matched_items": output.matched_items,
                    "exception_items": output.exception_items,
                    "unmatched_items": output.unmatched_items,
                },
            )
            ensure_reconciliation_phase()
        except JobCancellationRequestedError:
            session.rollback()
            raise
        except Exception as exc:
            msg = f"Reconciliation matching failed: {exc}"
            logger.exception(msg)
            session.rollback()
            raise RuntimeError(msg) from exc

        # Compute trial balance if requested
        trial_balance_computed = False
        trial_balance_balanced = False

        if trial_balance_requested:
            try:
                ensure_reconciliation_phase()
                account_balances = _compute_account_balances(session, parsed_close_run_id)

                snapshot = svc.compute_trial_balance(
                    close_run_id=parsed_close_run_id,
                    account_balances=account_balances,
                    generated_by_user_id=parsed_actor_user_id,
                    progress_guard=ensure_reconciliation_phase,
                )

                trial_balance_computed = True
                trial_balance_balanced = snapshot.is_balanced
                job_context.checkpoint(
                    step="compute_trial_balance",
                    state={
                        "trial_balance_computed": True,
                        "trial_balance_balanced": trial_balance_balanced,
                    },
                )
                ensure_reconciliation_phase()

                logger.info(
                    "Trial balance computed for close run %s: balanced=%s, debits=%s, credits=%s",
                    close_run_id,
                    trial_balance_balanced,
                    snapshot.total_debits,
                    snapshot.total_credits,
                )
            except JobCancellationRequestedError:
                session.rollback()
                raise
            except Exception as exc:
                msg = f"Trial balance computation failed: {exc}"
                logger.exception(msg)
                session.rollback()
                raise RuntimeError(msg) from exc

        # Commit all successful mutations before closing the session.
        try:
            ensure_reconciliation_phase()
            session.commit()
            job_context.checkpoint(
                step="persist_reconciliation_results",
                state={
                    "total_items": output.total_items,
                    "matched_items": output.matched_items,
                    "trial_balance_computed": trial_balance_computed,
                },
            )
        except JobCancellationRequestedError:
            session.rollback()
            raise
        except Exception as exc:
            msg = f"Failed to commit reconciliation results: {exc}"
            logger.exception(msg)
            session.rollback()
            raise RuntimeError(msg) from exc

    receipt = ReconciliationReceipt(
        close_run_id=close_run_id,
        reconciliation_types=reconciliation_types,
        total_items=output.total_items,
        matched_items=output.matched_items,
        exception_items=output.exception_items,
        unmatched_items=output.unmatched_items,
        trial_balance_computed=trial_balance_computed,
        trial_balance_balanced=trial_balance_balanced,
        errors=[],
    )

    logger.info(
        (
            "Reconciliation complete for close run %s: total=%d, matched=%d, "
            "exceptions=%d, unmatched=%d"
        ),
        close_run_id,
        receipt.total_items,
        receipt.matched_items,
        receipt.exception_items,
        receipt.unmatched_items,
    )

    return _reconciliation_receipt_to_payload(receipt)


def _reconciliation_receipt_to_payload(receipt: ReconciliationReceipt) -> dict[str, Any]:
    """Convert the slotted receipt dataclass into the JSON-safe task payload shape."""

    return {
        "close_run_id": receipt.close_run_id,
        "reconciliation_types": receipt.reconciliation_types,
        "total_items": receipt.total_items,
        "matched_items": receipt.matched_items,
        "exception_items": receipt.exception_items,
        "unmatched_items": receipt.unmatched_items,
        "trial_balance_computed": receipt.trial_balance_computed,
        "trial_balance_balanced": receipt.trial_balance_balanced,
        "errors": receipt.errors,
    }


@celery_app.task(
    bind=True,
    base=TrackedJobTask,
    name=TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN.value,
    autoretry_for=(),
    retry_backoff=False,
    retry_jitter=False,
    max_retries=resolve_task_route(TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN).max_retries,
)
def run_reconciliation(
    self: TrackedJobTask,
    *,
    close_run_id: str,
    reconciliation_types: list[str],
    actor_user_id: str | None = None,
    matching_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute reconciliation under the canonical checkpointed job wrapper."""

    return self.run_tracked_job(
        runner=lambda job_context: _run_reconciliation_task(
            close_run_id=close_run_id,
            reconciliation_types=reconciliation_types,
            actor_user_id=actor_user_id,
            matching_config=matching_config,
            job_context=job_context,
        )
    )


__all__ = [
    "ReconciliationReceipt",
    "run_reconciliation",
]
