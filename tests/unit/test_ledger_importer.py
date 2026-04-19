"""
Purpose: Verify canonical imported general-ledger row parsing and grouping-key derivation.
Scope: Focused unit coverage for explicit and derived transaction grouping behavior.
Dependencies: Ledger importer helpers only.
"""

from __future__ import annotations

from services.ledger.importer import import_general_ledger_file


def test_import_general_ledger_file_uses_explicit_transaction_group_column() -> None:
    """Rows sharing an explicit journal identifier should share one grouping key."""

    imported_file = import_general_ledger_file(
        filename="march-gl.csv",
        payload=(
            b"posting_date,account_code,journal_number,debit_amount,credit_amount\n"
            b"2026-03-05,1000,JE-1001,500.00,0.00\n"
            b"2026-03-05,4000,JE-1001,0.00,500.00\n"
        ),
    )

    first_line, second_line = imported_file.lines

    assert imported_file.import_metadata["transaction_grouping_strategy"] == "explicit_column"
    assert first_line.reference == "JE-1001"
    assert second_line.reference == "JE-1001"
    assert first_line.transaction_group_key == second_line.transaction_group_key
    assert first_line.transaction_group_key.startswith("glgrp_")


def test_import_general_ledger_file_derives_transaction_group_key_from_reference_fields() -> None:
    """Rows without an explicit grouping column should derive one from stable ledger fields."""

    imported_file = import_general_ledger_file(
        filename="march-gl.csv",
        payload=(
            b"posting_date,account_code,external_ref,debit_amount,credit_amount\n"
            b"2026-03-07,1000,BANK-DEP-001,250.00,0.00\n"
            b"2026-03-07,4000,BANK-DEP-001,0.00,250.00\n"
            b"2026-03-07,6100,,15.00,0.00\n"
        ),
    )

    first_line, second_line, third_line = imported_file.lines

    assert (
        imported_file.import_metadata["transaction_grouping_strategy"]
        == "derived_from_ledger_fields"
    )
    assert first_line.transaction_group_key == second_line.transaction_group_key
    assert third_line.transaction_group_key != first_line.transaction_group_key
