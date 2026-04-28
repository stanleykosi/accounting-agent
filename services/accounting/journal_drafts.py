"""
Purpose: Generate balanced journal entry drafts from accounting recommendations.
Scope: Convert validated recommendation payloads into journal draft inputs with balanced
debit/credit lines, narrative descriptions, dimension assignments, and evidence linkage.
Dependencies: Decimal arithmetic (no LLM math), canonical enums, recommendation contracts,
accounting rule engine outputs, and dimension helpers.

Design notes:
- This module owns the invariant that no journal draft can be created with unbalanced lines.
- All arithmetic uses Python Decimal; no floating-point or model-generated math is permitted.
- Journal drafts are created in DRAFT status and routed through approval logic (Step 28)
  before they can be applied to working accounting state.
- The journal generator supports both deterministic rule-based recommendations and
  model-assisted recommendations, but the balancing logic is always deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from services.accounting.dimensions import DimensionHelper, get_dimension_helper
from services.accounting.rules import AccountingTreatment
from services.common.enums import DocumentType
from services.contracts.journal_models import JournalDraftInput, JournalLineInput


class JournalDraftError(ValueError):
    """Represent a failure during journal draft generation."""

    def __init__(self, message: str) -> None:
        """Capture an operator-facing diagnostic message."""
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class JournalLineSpec:
    """Describe one debit or credit line for a journal draft."""

    line_no: int
    account_code: str
    line_type: str  # 'debit' or 'credit'
    amount: Decimal
    description: str | None = None
    dimensions: dict[str, str] | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class JournalDraftSpec:
    """Describe a complete balanced journal draft ready for persistence."""

    close_run_id: UUID
    entity_id: UUID
    recommendation_id: UUID | None
    posting_date: date
    description: str
    lines: tuple[JournalLineSpec, ...]
    reasoning_summary: str | None = None
    metadata_payload: dict[str, Any] | None = None
    source_surface: str = "system"

    @property
    def total_debits(self) -> Decimal:
        """Return the sum of all debit line amounts."""
        return sum(
            (line.amount for line in self.lines if line.line_type == "debit"),
            Decimal("0.00"),
        )

    @property
    def total_credits(self) -> Decimal:
        """Return the sum of all credit line amounts."""
        return sum(
            (line.amount for line in self.lines if line.line_type == "credit"),
            Decimal("0.00"),
        )

    @property
    def is_balanced(self) -> bool:
        """Return whether total debits equal total credits."""
        return self.total_debits == self.total_credits

    def validate(self) -> None:
        """Raise JournalDraftError if the draft is invalid or unbalanced.

        Checks performed:
        1. At least 2 lines are present.
        2. All line amounts are positive.
        3. Total debits equal total credits.
        4. Line numbers are unique.
        5. All account codes are non-empty.
        """
        if len(self.lines) < 2:
            raise JournalDraftError("Journal entries must have at least 2 lines.")

        if not self.is_balanced:
            raise JournalDraftError(
                f"Journal lines must balance. "
                f"Total debits: {self.total_debits}, Total credits: {self.total_credits}."
            )

        line_numbers: set[int] = set()
        for line in self.lines:
            if line.amount <= 0:
                raise JournalDraftError(
                    f"Line {line.line_no} amount must be positive, got {line.amount}."
                )
            if line.line_no in line_numbers:
                raise JournalDraftError(
                    f"Duplicate line number: {line.line_no}. Line numbers must be unique."
                )
            line_numbers.add(line.line_no)
            if not line.account_code.strip():
                raise JournalDraftError(f"Line {line.line_no} has an empty account code.")


def build_journal_draft_from_recommendation(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    recommendation_id: UUID,
    posting_date: date,
    payload: dict[str, Any],
    reasoning_summary: str,
    evidence_links: list[dict[str, Any]],
    rule_version: str,
    prompt_version: str,
    schema_version: str,
    dimension_helper: DimensionHelper | None = None,
) -> JournalDraftSpec:
    """Build a balanced journal draft spec from a validated recommendation payload.

    This function handles three recommendation payload shapes:
    1. `journal_lines` key with pre-constructed line items.
    2. `rule_evaluation` key from deterministic rule engine output.
    3. `account_code` + `amount` for simple single-account coding recommendations.

    Args:
        close_run_id: Close run the journal belongs to.
        entity_id: Entity workspace owning the journal.
        recommendation_id: Source recommendation UUID.
        posting_date: Accounting date for the journal.
        payload: Structured recommendation payload (varies by source).
        reasoning_summary: Human-readable explanation of why this journal was created.
        evidence_links: Structured evidence references for audit linkage.
        rule_version: Version of deterministic rules used.
        prompt_version: Version of prompt template used (if model-assisted).
        schema_version: Version of output schema conformed to.
        dimension_helper: Optional dimension helper for suggesting dimensions.

    Returns:
        A validated JournalDraftSpec ready for persistence.

    Raises:
        JournalDraftError: When the payload cannot produce a balanced journal.
    """
    dimensions = dimension_helper or get_dimension_helper()

    if "journal_lines" in payload:
        spec = _build_from_explicit_lines(
            close_run_id=close_run_id,
            entity_id=entity_id,
            recommendation_id=recommendation_id,
            posting_date=posting_date,
            payload=payload,
            reasoning_summary=reasoning_summary,
            dimensions=dimensions,
        )
    elif "rule_evaluation" in payload:
        spec = _build_from_rule_evaluation(
            close_run_id=close_run_id,
            entity_id=entity_id,
            recommendation_id=recommendation_id,
            posting_date=posting_date,
            payload=payload,
            reasoning_summary=reasoning_summary,
            dimensions=dimensions,
        )
    elif "account_code" in payload and "amount" in payload:
        spec = _build_from_simple_coding(
            close_run_id=close_run_id,
            entity_id=entity_id,
            recommendation_id=recommendation_id,
            posting_date=posting_date,
            payload=payload,
            reasoning_summary=reasoning_summary,
            dimensions=dimensions,
        )
    else:
        raise JournalDraftError(
            "Recommendation payload does not contain recognized journal data. "
            "Expected 'journal_lines', 'rule_evaluation', or 'account_code' + 'amount'."
        )

    # Attach metadata
    spec = JournalDraftSpec(
        close_run_id=spec.close_run_id,
        entity_id=spec.entity_id,
        recommendation_id=spec.recommendation_id,
        posting_date=spec.posting_date,
        description=spec.description,
        lines=spec.lines,
        reasoning_summary=spec.reasoning_summary,
        metadata_payload={
            "rule_version": rule_version,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "evidence_links": evidence_links,
            **(spec.metadata_payload or {}),
        },
        source_surface=spec.source_surface,
    )

    spec.validate()
    return spec


def build_journal_draft_input(
    *,
    spec: JournalDraftSpec,
) -> JournalDraftInput:
    """Convert a validated JournalDraftSpec into a Pydantic JournalDraftInput.

    This is the bridge between the internal draft spec and the validated Pydantic
    contract used for API serialization and persistence.

    Args:
        spec: A validated and balanced journal draft spec.

    Returns:
        JournalDraftInput ready for API route consumption.
    """
    lines = [
        JournalLineInput(
            line_no=line.line_no,
            account_code=line.account_code,
            line_type=line.line_type,
            amount=str(line.amount),
            description=line.description,
            dimensions=line.dimensions or {},
            reference=line.reference,
        )
        for line in spec.lines
    ]

    return JournalDraftInput(
        close_run_id=spec.close_run_id,
        entity_id=spec.entity_id,
        recommendation_id=spec.recommendation_id,
        posting_date=spec.posting_date,
        description=spec.description,
        lines=lines,
        reasoning_summary=spec.reasoning_summary,
        metadata_payload=spec.metadata_payload or {},
        source_surface=spec.source_surface,
    )


def _build_from_explicit_lines(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    recommendation_id: UUID,
    posting_date: date,
    payload: dict[str, Any],
    reasoning_summary: str,
    dimensions: DimensionHelper,
) -> JournalDraftSpec:
    """Build a journal draft from explicit line items in the recommendation payload."""
    raw_lines = payload["journal_lines"]
    if not isinstance(raw_lines, list) or len(raw_lines) < 2:
        raise JournalDraftError("journal_lines must contain at least 2 line items.")

    lines: list[JournalLineSpec] = []
    for idx, raw_line in enumerate(raw_lines, start=1):
        line_type = raw_line.get("line_type", "").lower()
        if line_type not in ("debit", "credit"):
            raise JournalDraftError(
                f"Line {idx} has invalid line_type '{raw_line.get('line_type')}'. "
                f"Must be 'debit' or 'credit'."
            )

        try:
            amount = Decimal(str(raw_line["amount"]))
        except (KeyError, ValueError, TypeError) as err:
            raise JournalDraftError(
                f"Line {idx} has an invalid or missing amount."
            ) from err

        if amount <= 0:
            raise JournalDraftError(f"Line {idx} amount must be positive, got {amount}.")

        account_code = str(raw_line.get("account_code", "")).strip()
        if not account_code:
            raise JournalDraftError(f"Line {idx} is missing an account_code.")

        lines.append(
            JournalLineSpec(
                line_no=idx,
                account_code=account_code,
                line_type=line_type,
                amount=amount,
                description=raw_line.get("description"),
                dimensions=raw_line.get("dimensions"),
                reference=raw_line.get("reference"),
            )
        )

    description = payload.get(
        "description",
        f"Journal entry from recommendation {recommendation_id}",
    )
    return JournalDraftSpec(
        close_run_id=close_run_id,
        entity_id=entity_id,
        recommendation_id=recommendation_id,
        posting_date=posting_date,
        description=str(description),
        lines=tuple(lines),
        reasoning_summary=reasoning_summary,
    )


def _build_from_rule_evaluation(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    recommendation_id: UUID,
    posting_date: date,
    payload: dict[str, Any],
    reasoning_summary: str,
    dimensions: DimensionHelper,
) -> JournalDraftSpec:
    """Build a journal draft from a deterministic rule evaluation payload.

    The rule evaluation contains:
    - account: the suggested GL account
    - amount: the transaction amount
    - treatment: the accounting treatment (accrual, prepayment, depreciation, standard)
    - dimensions: suggested dimension assignments

    For simple standard coding, we produce a two-line entry:
    - Debit to the suggested expense/asset account
    - Credit to a default bank/ap control account (from payload or context)
    """
    rule_eval = payload["rule_evaluation"]
    account_code = rule_eval.get("account_code")
    if not account_code:
        raise JournalDraftError("rule_evaluation must contain an account_code.")

    try:
        amount = Decimal(str(payload.get("amount", rule_eval.get("amount", "0"))))
    except (ValueError, TypeError) as err:
        raise JournalDraftError(
            "rule_evaluation or payload must contain a valid amount."
        ) from err

    if amount <= 0:
        raise JournalDraftError("Journal amount must be positive.")

    treatment = rule_eval.get("treatment", "standard_coding")
    offset_account = rule_eval.get(
        "offset_account",
        _default_offset_account_for_payload(
            treatment=treatment,
            document_type=payload.get("document_type"),
        ),
    )

    suggested_dims = rule_eval.get("dimensions", {})
    merged_dims = dimensions.merge_dimensions(
        base_dimensions=suggested_dims,
        override_dimensions=payload.get("dimensions", {}),
    )

    document_type = payload.get("document_type", "unknown")
    description = (
        f"{treatment.replace('_', ' ').title()} entry for {document_type} "
        f"on {posting_date.isoformat()}"
    )

    lines = (
        JournalLineSpec(
            line_no=1,
            account_code=account_code,
            line_type="debit",
            amount=amount,
            description=f"Debit to {account_code} for {document_type}",
            dimensions=merged_dims,
        ),
        JournalLineSpec(
            line_no=2,
            account_code=offset_account,
            line_type="credit",
            amount=amount,
            description=f"Credit to {offset_account} for {document_type}",
            dimensions=merged_dims,
        ),
    )

    return JournalDraftSpec(
        close_run_id=close_run_id,
        entity_id=entity_id,
        recommendation_id=recommendation_id,
        posting_date=posting_date,
        description=description,
        lines=lines,
        reasoning_summary=reasoning_summary,
    )


def _build_from_simple_coding(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    recommendation_id: UUID,
    posting_date: date,
    payload: dict[str, Any],
    reasoning_summary: str,
    dimensions: DimensionHelper,
) -> JournalDraftSpec:
    """Build a two-line journal draft from a simple account_code + amount recommendation.

    This is the minimal coding recommendation shape where we know:
    - which account to code to
    - how much the transaction is for

    We generate a balanced two-line entry with a default offset account.
    """
    account_code = str(payload["account_code"]).strip()
    if not account_code:
        raise JournalDraftError("account_code is required for simple coding recommendations.")

    try:
        amount = Decimal(str(payload["amount"]))
    except Exception as err:
        raise JournalDraftError(
            "amount is required and must be a valid decimal."
        ) from err

    if amount <= 0:
        raise JournalDraftError("amount must be positive for simple coding recommendations.")

    offset_account = payload.get(
        "offset_account",
        _default_offset_account_for_payload(
            treatment=AccountingTreatment.STANDARD_CODING.value,
            document_type=payload.get("document_type"),
        ),
    )

    merged_dims = dimensions.merge_dimensions(
        override_dimensions=payload.get("dimensions", {}),
    )

    document_type = payload.get("document_type", "transaction")
    description = (
        f"GL coding recommendation for {document_type} "
        f"-> {account_code} ({amount:.2f})"
    )

    lines = (
        JournalLineSpec(
            line_no=1,
            account_code=account_code,
            line_type="debit",
            amount=amount,
            description=f"Debit to {account_code}",
            dimensions=merged_dims,
        ),
        JournalLineSpec(
            line_no=2,
            account_code=offset_account,
            line_type="credit",
            amount=amount,
            description=f"Credit to {offset_account}",
            dimensions=merged_dims,
        ),
    )

    return JournalDraftSpec(
        close_run_id=close_run_id,
        entity_id=entity_id,
        recommendation_id=recommendation_id,
        posting_date=posting_date,
        description=description,
        lines=lines,
        reasoning_summary=reasoning_summary,
    )


def _default_offset_account_for_payload(
    *,
    treatment: str,
    document_type: Any,
) -> str:
    """Return the canonical offset account for one recommendation payload.

    The current product uses one deterministic default path until entity-specific
    settlement-account configuration exists. We intentionally avoid non-postable
    header accounts such as `1000 Assets`.
    """
    normalized_document_type = str(document_type or "").strip().lower()
    if treatment == AccountingTreatment.STANDARD_CODING.value or treatment == "standard_coding":
        document_defaults = {
            DocumentType.INVOICE.value: "2010",   # Accounts Payable
            DocumentType.RECEIPT.value: "1010",   # Operating Bank / Cash
            DocumentType.PAYSLIP.value: "2100",   # Accrued Expenses / Payroll clearing
        }
        return document_defaults.get(normalized_document_type, "1010")

    treatment_map = {
        AccountingTreatment.ACCRUAL.value: "2100",  # Accrued liabilities
        AccountingTreatment.PREPAYMENT.value: "1400",  # Prepaid expenses
        AccountingTreatment.DEPRECIATION.value: "1590",  # Accumulated depreciation
        "accrual": "2100",
        "prepayment": "1400",
        "depreciation": "1590",
    }
    return treatment_map.get(treatment, "1010")


def generate_journal_number(
    *,
    close_run_id: UUID,
    posting_date: date,
    sequence_no: int,
) -> str:
    """Generate a deterministic human-readable journal number.

    Format: JE-YYYY-NNNNN where YYYY is the posting year and NNNNN is the
    sequence number padded to 5 digits.

    Args:
        close_run_id: Close-run scope from the caller; numbering uses posting year and sequence.
        posting_date: Accounting date determining the year component.
        sequence_no: Sequential number within the year.

    Returns:
        A journal number string like 'JE-2026-00001'.
    """
    _ = close_run_id
    return f"JE-{posting_date.year}-{sequence_no:05d}"


__all__ = [
    "JournalDraftError",
    "JournalDraftSpec",
    "JournalLineSpec",
    "build_journal_draft_from_recommendation",
    "build_journal_draft_input",
    "generate_journal_number",
]
