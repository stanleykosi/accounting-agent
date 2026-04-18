"""
Purpose: Provide the canonical effective-ledger loaders shared by reconciliation and exports.
Scope: Imported baseline binding lookup, active COA account lookup, and transaction-level
effective ledger assembly from imported GL rows plus approved/applied close-run journals.
Dependencies: SQLAlchemy ORM models for close runs, COA, journals, and imported-ledger baselines.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from services.db.models.close_run import CloseRun
from services.db.models.coa import CoaAccount, CoaSet
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ledger import CloseRunLedgerBinding, GeneralLedgerImportLine
from sqlalchemy.orm import Session


def load_effective_ledger_transactions(
    session: Session,
    close_run_id: UUID,
) -> list[dict[str, Any]]:
    """Load effective transaction rows for one close run.

    The canonical effective ledger is defined as:
    1. imported GL baseline rows bound to the close run, when present
    2. plus approved/applied journal lines created inside this close run
    """

    transactions = load_imported_ledger_transactions(
        session,
        close_run_id,
    )
    transactions.extend(
        load_close_run_journal_transactions(
            session,
            close_run_id,
        )
    )
    return transactions


def load_close_run_journal_transactions(
    session: Session,
    close_run_id: UUID,
    *,
    account_lookup: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load approved/applied journal lines as effective-ledger transactions."""

    transactions: list[dict[str, Any]] = []
    resolved_account_lookup = account_lookup or {}
    journals = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.close_run_id == close_run_id,
            JournalEntry.status.in_(["approved", "applied"]),
        )
        .order_by(
            JournalEntry.posting_date.asc(),
            JournalEntry.created_at.asc(),
            JournalEntry.id.asc(),
        )
        .all()
    )

    for journal in journals:
        lines = (
            session.query(JournalLine)
            .filter(JournalLine.journal_entry_id == journal.id)
            .order_by(JournalLine.line_no.asc())
            .all()
        )
        for line in lines:
            amount = Decimal(str(line.amount))
            debit_amount = amount if line.line_type == "debit" else Decimal("0.00")
            credit_amount = amount if line.line_type == "credit" else Decimal("0.00")
            signed_amount = debit_amount if line.line_type == "debit" else Decimal("0.00") - amount
            account_name = resolved_account_lookup.get(line.account_code, {}).get("account_name")
            transactions.append(
                {
                    "ref": f"je:{journal.journal_number}:{line.line_no}",
                    "source_kind": "close_run_journal",
                    "source_record_id": str(journal.id),
                    "source_line_no": line.line_no,
                    "amount": str(amount),
                    "debit_amount": str(debit_amount),
                    "credit_amount": str(credit_amount),
                    "signed_amount": str(signed_amount),
                    "date": str(journal.posting_date),
                    "period": journal.posting_date.strftime("%Y-%m"),
                    "reference": line.reference or "",
                    "external_ref": "",
                    "account_code": line.account_code,
                    "account_name": account_name or "",
                    "description": line.description or "",
                    "dimensions": dict(line.dimensions or {}),
                    "line_type": line.line_type,
                    "journal_number": journal.journal_number,
                }
            )

    return transactions


def load_imported_ledger_transactions(
    session: Session,
    close_run_id: UUID,
    *,
    account_lookup: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load bound imported GL rows as effective-ledger transactions."""

    binding = load_close_run_ledger_binding(session, close_run_id)
    if binding is None or binding.general_ledger_import_batch_id is None:
        return []

    resolved_account_lookup = account_lookup or {}
    lines = (
        session.query(GeneralLedgerImportLine)
        .filter(GeneralLedgerImportLine.batch_id == binding.general_ledger_import_batch_id)
        .order_by(GeneralLedgerImportLine.posting_date.asc(), GeneralLedgerImportLine.line_no.asc())
        .all()
    )

    transactions: list[dict[str, Any]] = []
    for line in lines:
        debit_amount = Decimal(str(line.debit_amount))
        credit_amount = Decimal(str(line.credit_amount))
        signed_amount = debit_amount if debit_amount > 0 else Decimal("0.00") - credit_amount
        account_name = line.account_name or resolved_account_lookup.get(line.account_code, {}).get(
            "account_name",
            "",
        )
        transactions.append(
            {
                "ref": f"gl:{line.batch_id}:{line.line_no}",
                "source_kind": "imported_general_ledger",
                "source_record_id": str(line.batch_id),
                "source_line_no": line.line_no,
                "amount": str(debit_amount or credit_amount),
                "debit_amount": str(debit_amount),
                "credit_amount": str(credit_amount),
                "signed_amount": str(signed_amount),
                "date": str(line.posting_date),
                "period": line.posting_date.strftime("%Y-%m"),
                "reference": line.reference or line.external_ref or "",
                "external_ref": line.external_ref or "",
                "account_code": line.account_code,
                "account_name": account_name,
                "description": line.description or line.account_name or "",
                "dimensions": dict(line.dimensions or {}),
                "line_type": "debit" if debit_amount > 0 else "credit",
                "journal_number": "",
            }
        )

    return transactions


def load_close_run_ledger_binding(
    session: Session,
    close_run_id: UUID,
) -> CloseRunLedgerBinding | None:
    """Return the imported-ledger binding for one close run, if present."""

    return (
        session.query(CloseRunLedgerBinding)
        .filter(CloseRunLedgerBinding.close_run_id == close_run_id)
        .first()
    )


def load_active_coa_accounts(
    session: Session,
    close_run_id: UUID,
) -> dict[str, dict[str, Any]]:
    """Load the active chart-of-accounts account lookup for one close run."""

    close_run = session.query(CloseRun).filter(CloseRun.id == close_run_id).first()
    if close_run is None:
        return {}

    coa_set = (
        session.query(CoaSet)
        .filter(
            CoaSet.entity_id == close_run.entity_id,
            CoaSet.is_active,
        )
        .order_by(CoaSet.version_no.desc())
        .first()
    )
    if coa_set is None:
        return {}

    accounts = (
        session.query(CoaAccount)
        .filter(
            CoaAccount.coa_set_id == coa_set.id,
            CoaAccount.is_active,
        )
        .all()
    )
    return {
        account.account_code: {
            "account_code": account.account_code,
            "account_name": account.account_name,
            "account_type": account.account_type,
            "is_postable": account.is_postable,
        }
        for account in accounts
    }


__all__ = [
    "load_active_coa_accounts",
    "load_close_run_journal_transactions",
    "load_close_run_ledger_binding",
    "load_effective_ledger_transactions",
    "load_imported_ledger_transactions",
]
