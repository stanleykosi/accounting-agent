"""
Purpose: Verify that no unbalanced journal can pass validation or draft generation.
Scope: JournalDraftSpec validation, JournalDraftInput Pydantic validation, balanced/unbalanced
scenarios, edge cases around zero amounts, duplicate line numbers, and empty account codes.
Dependencies: pytest, Decimal, journal draft module, and journal contract models.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from services.accounting.journal_drafts import (
    JournalDraftError,
    JournalDraftSpec,
    JournalLineSpec,
    build_journal_draft_from_recommendation,
    build_journal_draft_input,
    generate_journal_number,
)
from services.contracts.journal_models import JournalDraftInput, JournalLineInput


class TestJournalDraftSpecValidation:
    """Validate the internal journal draft spec balancing logic."""

    def _make_balanced_spec(self) -> JournalDraftSpec:
        """Return a minimal balanced journal draft spec for testing."""
        return JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=uuid4(),
            posting_date=date(2026, 4, 12),
            description="Test balanced journal",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("1000.00"),
                    description="Debit expense",
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("1000.00"),
                    description="Credit bank",
                ),
            ),
        )

    def test_balanced_spec_passes_validation(self) -> None:
        """A balanced spec with positive amounts and unique line numbers validates cleanly."""
        spec = self._make_balanced_spec()
        spec.validate()  # Should not raise
        assert spec.is_balanced is True
        assert spec.total_debits == Decimal("1000.00")
        assert spec.total_credits == Decimal("1000.00")

    def test_unbalanced_spec_raises_error(self) -> None:
        """Debits != credits must fail validation."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Unbalanced journal",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("1500.00"),
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("1000.00"),
                ),
            ),
        )
        with pytest.raises(JournalDraftError, match="must balance"):
            spec.validate()

    def test_fewer_than_two_lines_raises_error(self) -> None:
        """Journal entries require at least 2 lines."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Too few lines",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("100.00"),
                ),
            ),
        )
        with pytest.raises(JournalDraftError, match="at least 2 lines"):
            spec.validate()

    def test_duplicate_line_numbers_raises_error(self) -> None:
        """Line numbers must be unique within a journal entry."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Duplicate line numbers",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("500.00"),
                ),
                JournalLineSpec(
                    line_no=1,  # Duplicate
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("500.00"),
                ),
            ),
        )
        with pytest.raises(JournalDraftError, match="Duplicate line number"):
            spec.validate()

    def test_negative_amount_raises_error(self) -> None:
        """Journal line amounts must be strictly positive."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Negative amount",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("-100.00"),
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("-100.00"),
                ),
            ),
        )
        with pytest.raises(JournalDraftError, match="must be positive"):
            spec.validate()

    def test_zero_amount_raises_error(self) -> None:
        """Zero amounts are not allowed in journal lines."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Zero amount",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("0.00"),
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("0.00"),
                ),
            ),
        )
        with pytest.raises(JournalDraftError, match="must be positive"):
            spec.validate()

    def test_empty_account_code_raises_error(self) -> None:
        """Empty account codes must be rejected."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Empty account code",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="",
                    line_type="debit",
                    amount=Decimal("100.00"),
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("100.00"),
                ),
            ),
        )
        with pytest.raises(JournalDraftError, match="empty account code"):
            spec.validate()

    def test_multi_line_balanced_journal(self) -> None:
        """A journal with more than 2 lines should validate when balanced."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=None,
            posting_date=date(2026, 4, 12),
            description="Multi-line journal",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("400.00"),
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="5100",
                    line_type="debit",
                    amount=Decimal("600.00"),
                ),
                JournalLineSpec(
                    line_no=3,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("1000.00"),
                ),
            ),
        )
        spec.validate()
        assert spec.is_balanced is True
        assert spec.total_debits == Decimal("1000.00")
        assert spec.total_credits == Decimal("1000.00")
        assert len(spec.lines) == 3


class TestJournalDraftInputValidation:
    """Validate the Pydantic contract for journal draft input."""

    def test_valid_balanced_input(self) -> None:
        """A balanced JournalDraftInput validates cleanly."""
        lines = [
            JournalLineInput(
                line_no=1,
                account_code="5000",
                line_type="debit",
                amount="1000.00",
            ),
            JournalLineInput(
                line_no=2,
                account_code="1000",
                line_type="credit",
                amount="1000.00",
            ),
        ]
        inp = JournalDraftInput(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            posting_date=date(2026, 4, 12),
            description="Balanced journal input",
            lines=lines,
        )
        assert inp.total_debits == Decimal("1000.00")
        assert inp.total_credits == Decimal("1000.00")

    def test_unbalanced_input_raises_validation_error(self) -> None:
        """Unbalanced lines must fail the Pydantic model validator."""
        lines = [
            JournalLineInput(
                line_no=1,
                account_code="5000",
                line_type="debit",
                amount="1500.00",
            ),
            JournalLineInput(
                line_no=2,
                account_code="1000",
                line_type="credit",
                amount="1000.00",
            ),
        ]
        with pytest.raises(ValueError, match="must balance"):
            JournalDraftInput(
                close_run_id=uuid4(),
                entity_id=uuid4(),
                posting_date=date(2026, 4, 12),
                description="Unbalanced input",
                lines=lines,
            )

    def test_invalid_amount_string_raises_error(self) -> None:
        """Non-numeric amount strings must be rejected."""
        with pytest.raises(ValueError, match="valid decimal string"):
            JournalLineInput(
                line_no=1,
                account_code="5000",
                line_type="debit",
                amount="not-a-number",
            )

    def test_negative_amount_string_raises_error(self) -> None:
        """Negative amounts must be rejected at the field level."""
        with pytest.raises(ValueError, match="must be strictly positive"):
            JournalLineInput(
                line_no=1,
                account_code="5000",
                line_type="debit",
                amount="-100.00",
            )

    def test_duplicate_line_numbers_in_input_raises_error(self) -> None:
        """Duplicate line numbers must be caught by the model validator."""
        lines = [
            JournalLineInput(
                line_no=1,
                account_code="5000",
                line_type="debit",
                amount="500.00",
            ),
            JournalLineInput(
                line_no=1,
                account_code="1000",
                line_type="credit",
                amount="500.00",
            ),
        ]
        with pytest.raises(ValueError, match="must be unique"):
            JournalDraftInput(
                close_run_id=uuid4(),
                entity_id=uuid4(),
                posting_date=date(2026, 4, 12),
                description="Duplicate lines",
                lines=lines,
            )

    def test_less_than_two_lines_raises_error(self) -> None:
        """Pydantic enforces min_length=2 on lines."""
        with pytest.raises(ValueError):
            JournalDraftInput(
                close_run_id=uuid4(),
                entity_id=uuid4(),
                posting_date=date(2026, 4, 12),
                description="Too few",
                lines=[
                    JournalLineInput(
                        line_no=1,
                        account_code="5000",
                        line_type="debit",
                        amount="100.00",
                    ),
                ],
            )


class TestBuildJournalDraftFromRecommendation:
    """Validate journal draft generation from recommendation payloads."""

    def _base_args(self) -> dict:
        return {
            "close_run_id": uuid4(),
            "entity_id": uuid4(),
            "recommendation_id": uuid4(),
            "posting_date": date(2026, 4, 12),
            "reasoning_summary": "Test reasoning",
            "evidence_links": [],
            "rule_version": "1.0.0",
            "prompt_version": "1.0.0",
            "schema_version": "1.0.0",
        }

    def test_from_explicit_journal_lines(self) -> None:
        """A recommendation with journal_lines produces a balanced draft."""
        args = self._base_args()
        args["payload"] = {
            "journal_lines": [
                {"line_type": "debit", "account_code": "5000", "amount": "2500.00"},
                {"line_type": "credit", "account_code": "1000", "amount": "2500.00"},
            ],
            "description": "From explicit lines",
        }
        spec = build_journal_draft_from_recommendation(**args)
        assert spec.is_balanced
        assert len(spec.lines) == 2
        spec.validate()

    def test_from_rule_evaluation(self) -> None:
        """A rule evaluation payload produces a two-line balanced draft."""
        args = self._base_args()
        args["payload"] = {
            "rule_evaluation": {
                "account_code": "5000",
                "amount": "750.00",
                "treatment": "standard_coding",
                "dimensions": {"cost_centre": "OPERATIONS"},
            },
            "amount": "750.00",
            "document_type": "invoice",
        }
        spec = build_journal_draft_from_recommendation(**args)
        assert spec.is_balanced
        assert len(spec.lines) == 2
        assert spec.lines[0].line_type == "debit"
        assert spec.lines[1].line_type == "credit"
        spec.validate()

    def test_invoice_rule_evaluation_defaults_offset_to_accounts_payable(self) -> None:
        """Invoice coding should default to AP, not the root Assets header account."""

        args = self._base_args()
        args["payload"] = {
            "rule_evaluation": {
                "account_code": "6050",
                "amount": "15480000.00",
                "treatment": "standard_coding",
                "dimensions": {},
            },
            "amount": "15480000.00",
            "document_type": "invoice",
        }

        spec = build_journal_draft_from_recommendation(**args)

        assert spec.lines[1].account_code == "2010"
        assert spec.lines[1].line_type == "credit"

    def test_from_simple_coding(self) -> None:
        """A simple account_code + amount produces a balanced two-line draft."""
        args = self._base_args()
        args["payload"] = {
            "account_code": "5000",
            "amount": "333.33",
            "document_type": "receipt",
        }
        spec = build_journal_draft_from_recommendation(**args)
        assert spec.is_balanced
        assert len(spec.lines) == 2
        spec.validate()

    def test_unrecognized_payload_raises_error(self) -> None:
        """A payload without recognized journal data must fail."""
        args = self._base_args()
        args["payload"] = {"some_unknown_key": "value"}
        with pytest.raises(JournalDraftError, match="does not contain recognized"):
            build_journal_draft_from_recommendation(**args)

    def test_missing_account_code_raises_error(self) -> None:
        """Simple coding without account_code must fail."""
        args = self._base_args()
        args["payload"] = {"amount": "100.00"}
        with pytest.raises(JournalDraftError):
            build_journal_draft_from_recommendation(**args)

    def test_invalid_amount_raises_error(self) -> None:
        """Simple coding with invalid amount must fail."""
        args = self._base_args()
        args["payload"] = {"account_code": "5000", "amount": "not-a-number"}
        with pytest.raises(JournalDraftError):
            build_journal_draft_from_recommendation(**args)

    def test_explicit_lines_too_few_raises_error(self) -> None:
        """Explicit journal_lines with fewer than 2 items must fail."""
        args = self._base_args()
        args["payload"] = {
            "journal_lines": [
                {"line_type": "debit", "account_code": "5000", "amount": "100.00"},
            ]
        }
        with pytest.raises(JournalDraftError, match="at least 2"):
            build_journal_draft_from_recommendation(**args)

    def test_explicit_lines_invalid_line_type_raises_error(self) -> None:
        """Invalid line_type in explicit lines must fail."""
        args = self._base_args()
        args["payload"] = {
            "journal_lines": [
                {"line_type": "debit", "account_code": "5000", "amount": "100.00"},
                {"line_type": "invalid", "account_code": "1000", "amount": "100.00"},
            ]
        }
        with pytest.raises(JournalDraftError, match="invalid line_type"):
            build_journal_draft_from_recommendation(**args)


class TestBuildJournalDraftInput:
    """Validate the conversion from JournalDraftSpec to JournalDraftInput."""

    def test_spec_converts_to_input(self) -> None:
        """A balanced spec converts to a valid Pydantic input."""
        spec = JournalDraftSpec(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            recommendation_id=uuid4(),
            posting_date=date(2026, 4, 12),
            description="Conversion test",
            lines=(
                JournalLineSpec(
                    line_no=1,
                    account_code="5000",
                    line_type="debit",
                    amount=Decimal("42.50"),
                    dimensions={"cost_centre": "HQ"},
                ),
                JournalLineSpec(
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount=Decimal("42.50"),
                    dimensions={"cost_centre": "HQ"},
                ),
            ),
        )
        inp = build_journal_draft_input(spec=spec)
        assert inp.total_debits == Decimal("42.50")
        assert inp.total_credits == Decimal("42.50")
        assert len(inp.lines) == 2
        assert inp.lines[0].dimensions == {"cost_centre": "HQ"}


class TestGenerateJournalNumber:
    """Validate deterministic journal number generation."""

    def test_format_is_correct(self) -> None:
        """Journal numbers follow JE-YYYY-NNNNN format."""
        number = generate_journal_number(
            close_run_id=uuid4(),
            posting_date=date(2026, 4, 12),
            sequence_no=1,
        )
        assert number == "JE-2026-00001"

    def test_sequence_pads_correctly(self) -> None:
        """Sequence numbers pad to 5 digits."""
        number = generate_journal_number(
            close_run_id=uuid4(),
            posting_date=date(2026, 1, 1),
            sequence_no=12345,
        )
        assert number == "JE-2026-12345"

    def test_year_changes_with_posting_date(self) -> None:
        """The year component reflects the posting date year."""
        number = generate_journal_number(
            close_run_id=uuid4(),
            posting_date=date(2027, 6, 15),
            sequence_no=42,
        )
        assert number == "JE-2027-00042"
