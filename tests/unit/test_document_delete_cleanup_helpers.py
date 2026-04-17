"""
Purpose: Verify delete-workflow storage cleanup helpers read nested parser derivative keys.
Scope: Repository-local helper coverage for document and entity deletion plans.
Dependencies: Raw parser payload helpers only.
"""

from __future__ import annotations

from services.db.repositories.document_repo import (
    _collect_raw_parse_derivative_keys as collect_document_keys,
)
from services.db.repositories.entity_repo import (
    _collect_raw_parse_derivative_keys as collect_entity_keys,
)


def test_document_delete_helper_reads_nested_extracted_tables_key() -> None:
    """Document delete cleanup should read extracted-table derivatives from the nested payload."""

    payload = {
        "text": "Invoice Number: INV-1048",
        "derivatives": {
            "normalized_storage_key": "documents/derivatives/doc-1/normalized.pdf",
            "ocr_text_storage_key": "documents/ocr/doc-1/text.txt",
            "extracted_tables_storage_key": "documents/derivatives/doc-1/tables.json",
        },
    }

    assert collect_document_keys(raw_parse_payload=payload) == (
        "documents/derivatives/doc-1/tables.json",
    )


def test_entity_delete_helper_reads_nested_extracted_tables_key() -> None:
    """Workspace delete cleanup should read extracted-table derivatives from the nested payload."""

    payload = {
        "text": "Bank Name: First Citizens Bank",
        "derivatives": {
            "normalized_storage_key": "documents/derivatives/doc-2/normalized.json",
            "ocr_text_storage_key": None,
            "extracted_tables_storage_key": "documents/derivatives/doc-2/tables.json",
        },
    }

    assert collect_entity_keys(raw_parse_payload=payload) == (
        "documents/derivatives/doc-2/tables.json",
    )
