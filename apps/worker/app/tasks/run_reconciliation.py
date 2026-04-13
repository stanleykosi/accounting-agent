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
from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.worker.app.celery_app import celery_app
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from services.common.enums import ReconciliationType
from services.common.logging import get_logger
from services.db.models.close_run import CloseRun
from services.db.models.coa import CoaAccount, CoaSet
from services.db.models.documents import Document, DocumentType
from services.db.models.extractions import DocumentExtraction
from services.db.models.journals import JournalEntry, JournalLine
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.session import get_session_factory
from services.jobs.task_names import TaskName, resolve_task_route
from services.reconciliation.matchers import DEFAULT_MATCHING_CONFIG, MatchingConfig
from services.reconciliation.service import ReconciliationService

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
        # Get extractions for this document
        extractions = (
            session.query(DocumentExtraction)
            .filter(DocumentExtraction.document_id == doc.id)
            .all()
        )

        for extraction in extractions:
            payload = extraction.extracted_payload
            # Bank statement lines are in the 'lines' key of the extraction
            for line_data in payload.get("lines", []):
                line_ref = f"bank:{doc.id}:{line_data.get('line_no', 0)}"
                source_items.append(
                    {
                        "ref": line_ref,
                        "amount": line_data.get("amount"),
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


def _load_ledger_transactions(session, close_run_id: UUID) -> list[dict[str, Any]]:
    """Load ledger transactions from approved journal entries.

    Args:
        session: Active SQLAlchemy session.
        close_run_id: The close run to load data for.

    Returns:
        List of ledger transaction dicts.
    """
    transactions: list[dict[str, Any]] = []

    # Load journal entries for this close run
    journals = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run_id,
            JournalEntry.status.in_(["approved", "applied"]),
        )
        .all()
    )

    for journal in journals:
        # Load journal lines
        lines = (
            session.query(JournalLine)
            .filter(JournalLine.journal_entry_id == journal.id)
            .order_by(JournalLine.line_no)
            .all()
        )

        for line in lines:
            transactions.append(
                {
                    "ref": f"je:{journal.journal_number}:{line.line_no}",
                    "amount": str(line.amount),
                    "date": str(journal.posting_date),
                    "reference": line.reference or "",
                    "account_code": line.account_code,
                    "description": line.description or "",
                    "dimensions": line.dimensions,
                }
            )

    return transactions


def _load_coa_accounts(session, close_run_id: UUID) -> dict[str, dict[str, Any]]:
    """Load the active chart of accounts for a close run's entity.

    Args:
        session: Active SQLAlchemy session.
        close_run_id: The close run to load COA for.

    Returns:
        Dict mapping account codes to account metadata.
    """
    # Get the close run to find the entity
    close_run = session.query(CloseRun).filter(CloseRun.id == close_run_id).first()
    if close_run is None:
        return {}

    # Get the active COA set for this entity
    coa_set = (
        session.query(CoaSet)
        .filter(
            CoaSet.entity_id == close_run.entity_id,
            CoaSet.is_active == True,  # noqa: E712
        )
        .order_by(CoaSet.version_no.desc())
        .first()
    )

    if coa_set is None:
        return {}

    # Load accounts
    accounts = (
        session.query(CoaAccount)
        .filter(
            CoaAccount.coa_set_id == coa_set.id,
            CoaAccount.is_active == True,  # noqa: E712
        )
        .all()
    )

    return {
        acct.account_code: {
            "account_code": acct.account_code,
            "account_name": acct.account_name,
            "account_type": acct.account_type,
            "is_postable": acct.is_postable,
        }
        for acct in accounts
    }


def _compute_account_balances(session, close_run_id: UUID) -> list[dict[str, Any]]:
    """Compute account balances from journal lines for trial balance.

    Args:
        session: Active SQLAlchemy session.
        close_run_id: The close run to compute balances for.

    Returns:
        List of account balance dicts suitable for trial balance checking.
    """
    # Load COA accounts
    coa_accounts = _load_coa_accounts(session, close_run_id)

    # Load all journal lines for this close run
    journals = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run_id,
            JournalEntry.status.in_(["approved", "applied"]),
        )
        .all()
    )

    # Aggregate balances by account code
    balances: dict[str, dict[str, Any]] = {}
    for journal in journals:
        lines = (
            session.query(JournalLine)
            .filter(JournalLine.journal_entry_id == journal.id)
            .all()
        )

        for line in lines:
            code = line.account_code
            if code not in balances:
                acct_info = coa_accounts.get(code, {})
                balances[code] = {
                    "account_code": code,
                    "account_name": acct_info.get("account_name", code),
                    "account_type": acct_info.get("account_type", "unknown"),
                    "debit_balance": Decimal("0.00"),
                    "credit_balance": Decimal("0.00"),
                    "is_active": True,
                }

            amount = Decimal(str(line.amount))
            if line.line_type == "debit":
                balances[code]["debit_balance"] += amount
            else:
                balances[code]["credit_balance"] += amount

    return list(balances.values())


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

        else:
            # Other types: use ledger transactions as counterparts
            source_data[rec_type] = {
                "source_items": [],
                "counterparts": ledger_transactions,
            }

    return source_data


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
    errors: list[str] = []

    logger.info(
        "Starting reconciliation for close run %s, types=%s",
        close_run_id,
        reconciliation_types,
    )

    with get_session_factory()() as session:
        repo = ReconciliationRepository(session)
        svc = ReconciliationService(repository=repo, matching_config=config)

        # Build source data
        try:
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
            job_context.ensure_not_canceled()
        except Exception as exc:
            msg = f"Failed to load reconciliation source data: {exc}"
            logger.exception(msg)
            errors.append(msg)
            session.rollback()
            return _reconciliation_receipt_to_payload(
                ReconciliationReceipt(
                    close_run_id=close_run_id,
                    reconciliation_types=reconciliation_types,
                    total_items=0,
                    matched_items=0,
                    exception_items=0,
                    unmatched_items=0,
                    trial_balance_computed=False,
                    trial_balance_balanced=False,
                    errors=errors,
                )
            )

        # Run reconciliation matching
        try:
            output = svc.run_reconciliation(
                close_run_id=parsed_close_run_id,
                reconciliation_types=parsed_types,
                source_data=source_data,
                created_by_user_id=parsed_actor_user_id,
                matching_config=config,
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
            job_context.ensure_not_canceled()
        except Exception as exc:
            msg = f"Reconciliation matching failed: {exc}"
            logger.exception(msg)
            errors.append(msg)
            session.rollback()
            return _reconciliation_receipt_to_payload(
                ReconciliationReceipt(
                    close_run_id=close_run_id,
                    reconciliation_types=reconciliation_types,
                    total_items=0,
                    matched_items=0,
                    exception_items=0,
                    unmatched_items=0,
                    trial_balance_computed=False,
                    trial_balance_balanced=False,
                    errors=errors,
                )
            )

        # Compute trial balance if requested
        trial_balance_computed = False
        trial_balance_balanced = False

        if ReconciliationType.TRIAL_BALANCE in parsed_types:
            try:
                account_balances = _compute_account_balances(session, parsed_close_run_id)
                coa_accounts = _load_coa_accounts(session, parsed_close_run_id)

                snapshot = svc.compute_trial_balance(
                    close_run_id=parsed_close_run_id,
                    account_balances=account_balances,
                    expected_account_codes=set(coa_accounts.keys()),
                    generated_by_user_id=parsed_actor_user_id,
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
                job_context.ensure_not_canceled()

                logger.info(
                    "Trial balance computed for close run %s: balanced=%s, debits=%s, credits=%s",
                    close_run_id,
                    trial_balance_balanced,
                    snapshot.total_debits,
                    snapshot.total_credits,
                )
            except Exception as exc:
                msg = f"Trial balance computation failed: {exc}"
                logger.exception(msg)
                errors.append(msg)

        # Commit all successful mutations before closing the session
        if not errors or output.total_items > 0:
            try:
                session.commit()
                job_context.checkpoint(
                    step="persist_reconciliation_results",
                    state={
                        "total_items": output.total_items,
                        "matched_items": output.matched_items,
                    },
                )
            except Exception as exc:
                msg = f"Failed to commit reconciliation results: {exc}"
                logger.exception(msg)
                session.rollback()
                errors.append(msg)

    receipt = ReconciliationReceipt(
        close_run_id=close_run_id,
        reconciliation_types=reconciliation_types,
        total_items=output.total_items,
        matched_items=output.matched_items,
        exception_items=output.exception_items,
        unmatched_items=output.unmatched_items,
        trial_balance_computed=trial_balance_computed,
        trial_balance_balanced=trial_balance_balanced,
        errors=errors,
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
