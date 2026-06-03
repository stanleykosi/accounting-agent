"""
Purpose: Verify canonical imported general-ledger row parsing and grouping-key derivation.
Scope: Focused unit coverage for explicit and derived transaction grouping behavior.
Dependencies: Ledger importer helpers only.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from services.imports.intelligence import IMPORT_INTELLIGENCE_METADATA_KEY
from services.ledger.importer import import_general_ledger_file, import_trial_balance_file


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
    report = imported_file.import_metadata[IMPORT_INTELLIGENCE_METADATA_KEY]
    assert isinstance(report, dict)
    assert report["document_kind"] == "general_ledger"
    assert report["header_row_number"] == 1
    assert report["transaction_grouping_strategy"] == "explicit_column"
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


def test_import_trial_balance_file_accepts_title_rows_and_account_name_layout() -> None:
    """Real-world TB exports often include report titles and account names instead of codes."""

    imported_file = import_trial_balance_file(
        filename="trial-balance.csv",
        payload=(
            b"Company Name,,\n"
            b"Trial Balance as at 2026-03-31,,\n"
            b"Account,Debit,Credit\n"
            b"Current Account,37860.47,\n"
            b"Creditors,,11523.54\n"
            b"TOTAL,73547.01,73547.01\n"
        ),
        account_code_lookup={
            "Current Account": "100",
            "Creditors": "1000",
        },
    )

    assert imported_file.import_metadata["account_identity_strategy"] == "resolved_by_name"
    report = imported_file.import_metadata[IMPORT_INTELLIGENCE_METADATA_KEY]
    assert isinstance(report, dict)
    assert report["document_kind"] == "trial_balance"
    assert report["header_row_number"] == 3
    assert report["account_identity_strategy"] == "resolved_by_name"
    assert report["account_name_resolution_count"] == 2
    assert report["skipped_summary_row_count"] == 1
    assert len(imported_file.lines) == 2
    assert imported_file.lines[0].account_code == "100"
    assert imported_file.lines[0].debit_balance == Decimal("37860.47")
    assert imported_file.lines[1].account_code == "1000"


def test_import_trial_balance_file_accepts_searchable_pdf_table_fixture() -> None:
    """Searchable PDF tables should import when text extraction preserves delimiters."""

    fixture = Path(
        "tests/fixtures/enterprise-close-pack-ngn/ledger/"
        "apex-meridian-trial-balance-2026-03.pdf"
    )

    imported_file = import_trial_balance_file(filename=fixture.name, payload=fixture.read_bytes())

    assert imported_file.import_metadata["format"] == "pdf"
    assert len(imported_file.lines) == 40
    assert imported_file.lines[0].account_code == "1010"


def test_import_general_ledger_file_accepts_searchable_pdf_table_fixture() -> None:
    """Searchable PDF GL tables should import when text extraction preserves delimiters."""

    fixture = Path(
        "tests/fixtures/enterprise-close-pack-ngn/ledger/"
        "apex-meridian-general-ledger-2026-03.pdf"
    )

    imported_file = import_general_ledger_file(filename=fixture.name, payload=fixture.read_bytes())

    assert imported_file.import_metadata["format"] == "pdf"
    assert len(imported_file.lines) == 46
    assert imported_file.lines[0].posting_date.isoformat() == "2026-03-02"
