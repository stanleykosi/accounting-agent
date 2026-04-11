"""
Purpose: Unit tests for extraction evidence reference builders.
Scope: Tests for evidence reference creation, normalization, and snippet
generation.
Dependencies: Extraction schemas and evidence reference builders.
"""

import pytest
from services.extraction.evidence_refs import (
    build_evidence_ref,
    build_pdf_evidence_ref,
    build_spreadsheet_evidence_ref,
    build_table_evidence_ref,
    merge_snippet_context,
    normalize_parser_output_to_evidence_ref,
)


class TestBuildEvidenceRef:
    """Tests for build_evidence_ref helper."""

    def test_build_with_all_fields(self):
        """Evidence reference builds with all fields set."""
        ref = build_evidence_ref(
            page=1,
            row=5,
            cell="C10",
            x_coordinate=100.5,
            y_coordinate=200.75,
            snippet="Total due: NGN 50,000.00",
        )

        assert ref.page == 1
        assert ref.row == 5
        assert ref.cell == "C10"
        assert ref.x_coordinate == 100.5
        assert ref.y_coordinate == 200.75
        assert ref.snippet == "Total due: NGN 50,000.00"

    def test_build_with_minimal_fields(self):
        """Evidence reference builds with only snippet."""
        ref = build_evidence_ref(snippet="Invoice #INV-001")

        assert ref.page is None
        assert ref.row is None
        assert ref.cell is None
        assert ref.x_coordinate is None
        assert ref.y_coordinate is None
        assert ref.snippet == "Invoice #INV-001"

    def test_build_with_no_fields(self):
        """Evidence reference builds with all None."""
        ref = build_evidence_ref()

        assert ref.page is None
        assert ref.row is None
        assert ref.cell is None
        assert ref.x_coordinate is None
        assert ref.y_coordinate is None
        assert ref.snippet is None


class TestBuildPdfEvidenceRef:
    """Tests for PDF-specific evidence builder."""

    def test_build_pdf_evidence_ref(self):
        """PDF evidence reference builds with page and coordinates."""
        ref = build_pdf_evidence_ref(
            page=3,
            x_coordinate=150.25,
            y_coordinate=300.5,
            snippet="NGN 1,250,000.00",
        )

        assert ref.page == 3
        assert ref.x_coordinate == 150.25
        assert ref.y_coordinate == 300.5
        assert ref.snippet == "NGN 1,250,000.00"


class TestBuildTableEvidenceRef:
    """Tests for table evidence builder."""

    def test_build_table_with_column(self):
        """Table evidence reference builds with column letter."""
        ref = build_table_evidence_ref(
            page=2,
            row=10,
            column=4,
            snippet="Bank transfer",
        )

        assert ref.page == 2
        assert ref.row == 10
        assert ref.cell == "D10"
        assert ref.snippet == "Bank transfer"

    def test_build_table_without_column(self):
        """Table evidence reference builds without column."""
        ref = build_table_evidence_ref(page=1, row=5)

        assert ref.page == 1
        assert ref.row == 5
        assert ref.cell is None


class TestBuildSpreadsheetEvidenceRef:
    """Tests for spreadsheet evidence builder."""

    def test_build_spreadsheet_evidence_ref(self):
        """Spreadsheet evidence reference builds from cell."""
        ref = build_spreadsheet_evidence_ref(
            cell="B15",
            snippet="50,000.00",
        )

        assert ref.cell == "B15"
        assert ref.snippet == "50,000.00"


class TestNormalizeParserOutputToEvidenceRef:
    """Tests for parser output normalization."""

    def test_normalize_pdf_output(self):
        """PDF parser output normalizes correctly."""
        parser_output = {
            "source_type": "pdf",
            "page": 2,
            "x": 120.5,
            "y": 240.0,
            "text": "Total: 250,000",
        }

        ref = normalize_parser_output_to_evidence_ref(parser_output)

        assert ref.page == 2
        assert ref.x_coordinate == 120.5
        assert ref.y_coordinate == 240.0
        assert ref.snippet == "Total: 250,000"

    def test_normalize_table_output(self):
        """Table parser output normalizes correctly."""
        parser_output = {
            "source_type": "table",
            "page": 1,
            "row": 8,
            "col": 3,
            "cell_text": "Description of item",
        }

        ref = normalize_parser_output_to_evidence_ref(parser_output)

        assert ref.page == 1
        assert ref.row == 8
        assert ref.cell == "C8"
        assert ref.snippet == "Description of item"

    def test_normalize_excel_output(self):
        """Excel parser output normalizes correctly."""
        parser_output = {
            "source_type": "excel",
            "cell": "D12",
            "value": "125000.50",
        }

        ref = normalize_parser_output_to_evidence_ref(parser_output)

        assert ref.cell == "D12"
        assert ref.snippet == "125000.50"

    def test_normalize_csv_output(self):
        """CSV parser output normalizes correctly."""
        parser_output = {
            "source_type": "csv",
            "cell": "A1",
            "value": "Invoice #INV-001",
        }

        ref = normalize_parser_output_to_evidence_ref(parser_output)

        assert ref.cell == "A1"
        assert ref.snippet == "Invoice #INV-001"

    def test_normalize_unknown_output(self):
        """Unknown source type returns minimal reference."""
        parser_output = {
            "source_type": "unknown",
            "text": "Some text",
        }

        ref = normalize_parser_output_to_evidence_ref(parser_output)

        assert ref.snippet == "Some text"

    def test_normalize_empty_output(self):
        """Empty parser output returns empty reference."""
        parser_output = {}

        ref = normalize_parser_output_to_evidence_ref(parser_output)

        assert ref.page is None
        assert ref.cell is None
        assert ref.snippet is None


class TestMergeSnippetContext:
    """Tests for snippet context merging."""

    def test_merge_with_full_context(self):
        """Snippet merges with full context around target."""
        base = "The quick brown fox jumps over the lazy dog. Total amount: 50000.00 is the sum due."
        target_value = "50000.00"

        result = merge_snippet_context(base, target_value, context_length=20)

        assert "50000.00" in result
        assert len(result) <= len(target_value) + 40

    def test_merge_at_start_of_text(self):
        """Snippet handles target at start of text."""
        base = "50000.00 is the total due."
        target_value = "50000.00"

        result = merge_snippet_context(base, target_value, context_length=20)

        assert result == "50000.00 is the total due."

    def test_merge_at_end_of_text(self):
        """Snippet handles target at end of text."""
        base = "The total due is 50000.00"
        target_value = "50000.00"

        result = merge_snippet_context(base, target_value, context_length=20)

        assert result == "The total due is 50000.00"

    def test_merge_target_not_found(self):
        """Snippet returns trimmed text when target not found."""
        base = "This is a longer text without the target value we are looking for in this test."
        target_value = "missing_value"

        result = merge_snippet_context(base, target_value, context_length=20)

        assert "missing_value" not in result

    def test_merge_empty_inputs(self):
        """Snippet handles empty inputs gracefully."""
        result = merge_snippet_context("", "value", context_length=50)
        assert result == ""

        result = merge_snippet_context("text", "", context_length=50)
        assert result == "text"

        result = merge_snippet_context("", "", context_length=50)
        assert result == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
