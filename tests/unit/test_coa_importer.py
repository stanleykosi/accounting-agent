"""
Purpose: Verify real-world chart-of-accounts import layouts.
Scope: Header-row detection and account-type inference for non-template COA exports.
Dependencies: COA importer only.
"""

from __future__ import annotations

from pathlib import Path

from services.coa.importer import import_coa_file
from services.imports.intelligence import IMPORT_INTELLIGENCE_METADATA_KEY


def test_import_coa_file_accepts_title_rows_and_statement_group_layout() -> None:
    """COA exports can use Code/Group/Normally instead of the canonical template headers."""

    imported_file = import_coa_file(
        filename="chart-of-accounts.csv",
        payload=(
            b"Sample Chart of Accounts,,,,,\n"
            b"Account Name,Code,Financial Statement,Group,Sub-Group,Normally\n"
            b"Bank checking account,100,Balance sheet,Current assets,"
            b"Cash and cash equivalents,Debit\n"
            b"Accounts payable,1000,Balance sheet,Current liabilities,Accounts payable,Credit\n"
            b"Sales,4000,Income statement,Revenue,Sales,Credit\n"
            b"Rent Expense,6020,Income statement,Operating Expenses,Rent Expense,Debit\n"
        ),
    )

    assert len(imported_file.accounts) == 4
    assert imported_file.accounts[0].account_code == "100"
    assert imported_file.accounts[0].account_type == "asset"
    assert imported_file.accounts[1].account_type == "liability"
    assert imported_file.accounts[2].account_type == "revenue"
    assert imported_file.accounts[3].account_type == "expense"
    report = imported_file.import_metadata[IMPORT_INTELLIGENCE_METADATA_KEY]
    assert isinstance(report, dict)
    assert report["document_kind"] == "chart_of_accounts"
    assert report["header_row_number"] == 2
    assert report["status"] == "parsed_with_warnings"
    assert report["account_type_strategy"] == "inferred_from_statement_group"


def test_import_coa_file_accepts_searchable_pdf_table_fixture() -> None:
    """Searchable PDFs with preserved table separators should import like workbooks."""

    fixture = Path("tests/fixtures/enterprise-close-pack-ngn/coa/apex-meridian-enterprise-coa.pdf")

    imported_file = import_coa_file(filename=fixture.name, payload=fixture.read_bytes())

    assert imported_file.import_metadata["format"] == "pdf"
    report = imported_file.import_metadata[IMPORT_INTELLIGENCE_METADATA_KEY]
    assert isinstance(report, dict)
    assert report["source_format"] == "pdf"
    assert len(imported_file.accounts) == 49
    assert imported_file.accounts[0].account_code == "1000"
