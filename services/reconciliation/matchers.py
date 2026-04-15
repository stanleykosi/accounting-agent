"""
Purpose: Implement exact and fuzzy matching helpers for reconciliation workflows.
Scope: Bank reconciliation matching (exact and fuzzy), AR/AP ageing matching,
       intercompany balance matching, payroll control reconciliation, fixed asset
       register reconciliation, loan amortisation reconciliation, accrual tracker
       reconciliation, budget vs actual reconciliation, and trial balance checks.
Dependencies: Decimal for math-safe arithmetic, canonical enums, extraction schemas,
       accounting rules, and journal models.

Design notes:
- All matching uses Decimal arithmetic — never delegates to LLM.
- Exact matching requires precise amount and reference match within tolerances.
- Fuzzy matching uses configurable tolerances for amount, date, and reference similarity.
- Match results carry confidence scores so review queues can surface low-confidence items.
- Each matcher returns a uniform MatchResult so the reconciliation service can
  aggregate outcomes across all reconciliation types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, ClassVar, Protocol

from services.common.enums import (
    AccountType,
    AnomalyType,
    MatchStatus,
    ReconciliationSourceType,
    ReconciliationType,
)
from services.common.types import JsonObject

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Matching configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchingConfig:
    """Hold matching thresholds and tolerances used by all reconciliation matchers.

    Attributes:
        exact_amount_tolerance: Maximum absolute difference for exact amount matching.
        fuzzy_amount_tolerance_pct: Percentage tolerance for fuzzy amount matching.
        date_tolerance_days: Maximum day difference for date-based fuzzy matching.
        reference_match_strict: Whether reference matching requires exact string equality.
        confidence_high: Threshold above which a match is considered high confidence.
        confidence_low: Threshold below which a match is considered low confidence.
    """

    exact_amount_tolerance: Decimal = Decimal("0.00")
    fuzzy_amount_tolerance_pct: float = 1.0
    date_tolerance_days: int = 5
    reference_match_strict: bool = True
    confidence_high: float = 0.9
    confidence_low: float = 0.5


DEFAULT_MATCHING_CONFIG = MatchingConfig()


def _normalize_dimension_value(value: Any) -> str:
    """Normalize an optional reconciliation dimension into a stable string."""

    return str(value or "").strip()


def _budget_dimension_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Build the canonical budget grouping key including all supported dimensions."""

    return (
        str(item.get("account_code", "")).strip(),
        str(item.get("period", "")).strip(),
        _normalize_dimension_value(item.get("department")),
        _normalize_dimension_value(item.get("cost_centre")),
        _normalize_dimension_value(item.get("project")),
    )


def _build_budget_reference(
    *,
    prefix: str,
    account_code: str,
    period: str,
    department: str,
    cost_centre: str,
    project: str,
) -> str:
    """Build a stable budget reference that preserves dimensional splits."""

    reference_parts = [prefix, account_code, period]
    reference_parts.extend(
        dimension
        for dimension in (department, cost_centre, project)
        if dimension
    )
    return ":".join(reference_parts)

# ---------------------------------------------------------------------------
# Core match result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchCounterpart:
    """Represent one counterpart that a source item was matched to.

    Attributes:
        source_type: The type of the counterpart (ledger_transaction, bank_statement_line, etc.).
        source_ref: Reference identifier for the counterpart.
        amount: Counterpart amount, if applicable.
        date: Counterpart date, if applicable.
        confidence: Match confidence between 0 and 1.
        match_reason: Brief explanation of why this counterpart matched.
    """

    source_type: ReconciliationSourceType
    source_ref: str
    amount: Decimal | None = None
    date: date | None = None
    confidence: float = 1.0
    match_reason: str = ""


@dataclass(frozen=True)
class MatchResult:
    """Represent the outcome of matching one source item against potential counterparts.

    Attributes:
        source_ref: Reference of the source item being matched.
        source_type: Type of the source item.
        source_amount: Amount of the source item.
        match_status: The matching outcome (matched, partially_matched, unmatched, exception).
        counterparts: List of counterparts matched to this source item.
        difference_amount: Monetary difference between source and counterpart(s).
        confidence: Aggregate match confidence between 0 and 1.
        explanation: Human-readable explanation of the match outcome.
        requires_disposition: Whether a reviewer must disposition this item.
        metadata: Additional structured context for the match.
    """

    source_ref: str
    source_type: ReconciliationSourceType
    source_amount: Decimal
    match_status: MatchStatus
    counterparts: list[MatchCounterpart] = field(default_factory=list)
    difference_amount: Decimal = Decimal("0.00")
    confidence: float = 0.0
    explanation: str = ""
    requires_disposition: bool = False
    metadata: JsonObject = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Matching protocol
# ---------------------------------------------------------------------------


class MatcherProtocol(Protocol):
    """Define the interface all reconciliation matchers must implement."""

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match source items against counterparts and return results.

        Args:
            source_items: List of source item dicts to match from.
            counterparts: List of counterpart dicts to match against.
            config: Optional matching configuration overrides.

        Returns:
            List of MatchResult objects, one per source item.
        """
        ...


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _parse_amount(value: str | int | float | Decimal | None) -> Decimal | None:
    """Safely parse an amount from various input types into Decimal.

    Args:
        value: The amount value to parse.

    Returns:
        Decimal amount or None if parsing fails.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("Failed to parse amount %r, returning None.", value)
        return None


def _parse_date(value: str | date | datetime | None) -> date | None:
    """Safely parse a date from various input types.

    Args:
        value: The date value to parse.

    Returns:
        Parsed date or None if parsing fails.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except (ValueError, TypeError):
                continue
    return None


def _compute_amount_confidence(diff: Decimal, amount: Decimal) -> float:
    """Compute a confidence score based on the relative amount difference.

    Args:
        diff: Absolute difference between amounts.
        amount: The source amount used as the baseline.

    Returns:
        Confidence score between 0 and 1.
    """
    if amount == 0:
        return 1.0 if diff == 0 else 0.0
    pct = abs(diff / amount) * 100
    if pct == 0:
        return 1.0
    if pct <= 0.1:
        return 0.95
    if pct <= 0.5:
        return 0.85
    if pct <= 1.0:
        return 0.7
    if pct <= 2.0:
        return 0.5
    if pct <= 5.0:
        return 0.3
    return 0.0


def _compute_date_confidence(date_diff: int, tolerance_days: int) -> float:
    """Compute a confidence score based on date proximity.

    Args:
        date_diff: Absolute day difference between dates.
        tolerance_days: Maximum acceptable day difference.

    Returns:
        Confidence score between 0 and 1.
    """
    if date_diff == 0:
        return 1.0
    if date_diff <= tolerance_days:
        return max(0.5, 1.0 - (date_diff / tolerance_days) * 0.5)
    return 0.0


def _reference_similarity(ref_a: str, ref_b: str) -> float:
    """Compute string similarity between two reference identifiers.

    Uses SequenceMatcher for ratio-based similarity. Returns value between 0 and 1.

    Args:
        ref_a: First reference string.
        ref_b: Second reference string.

    Returns:
        Similarity ratio between 0 and 1.
    """
    if not ref_a or not ref_b:
        return 0.0
    a = ref_a.strip().lower()
    b = ref_b.strip().lower()
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Bank reconciliation matcher
# ---------------------------------------------------------------------------


class BankReconciliationMatcher:
    """Match bank statement lines against ledger transactions.

    This matcher implements a two-phase approach:
    1. Exact matching: Match on amount + reference or amount + date within tolerance.
    2. Fuzzy matching: Match remaining items using configurable tolerances.

    The matcher handles:
    - One-to-one matches (most common)
    - One-to-many matches (one bank line splits across multiple ledger entries)
    - Many-to-one matches (multiple bank lines aggregate to one ledger entry)
    """

    reconciliation_type = ReconciliationType.BANK_RECONCILIATION

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match bank statement lines to ledger transactions.

        Args:
            source_items: Bank statement line dicts with keys: ref, amount, date, reference.
            counterparts: Ledger transaction dicts with keys: ref, amount, date, reference.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects, one per bank statement line.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []
        matched_counterpart_refs: set[str] = set()

        # Phase 1: Exact matching
        for item in source_items:
            result = self._try_exact_match(item, counterparts, matched_counterpart_refs, effective_config)
            if result is not None:
                results.append(result)

        # Phase 2: Fuzzy matching for unmatched items
        matched_source_refs = {r.source_ref for r in results}
        for item in source_items:
            item_ref = item.get("ref", "")
            if item_ref in matched_source_refs:
                continue
            result = self._try_fuzzy_match(item, counterparts, matched_counterpart_refs, effective_config)
            results.append(result)

        # Phase 3: Mark unmatched counterparts for investigation
        for cp in counterparts:
            cp_ref = cp.get("ref", "")
            if cp_ref not in matched_counterpart_refs:
                results.append(
                    MatchResult(
                        source_ref=f"unmatched_ledger:{cp_ref}",
                        source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                        source_amount=_parse_amount(cp.get("amount")) or Decimal("0.00"),
                        match_status=MatchStatus.UNMATCHED,
                        counterparts=[],
                        difference_amount=_parse_amount(cp.get("amount")) or Decimal("0.00"),
                        confidence=0.0,
                        explanation=f"Ledger transaction {cp_ref!r} has no matching bank statement line.",
                        requires_disposition=True,
                        metadata={"counterpart_ref": cp_ref},
                    )
                )

        return results

    def _try_exact_match(
        self,
        item: dict[str, Any],
        counterparts: list[dict[str, Any]],
        matched_refs: set[str],
        config: MatchingConfig,
    ) -> MatchResult | None:
        """Attempt exact matching of one bank statement line.

        Args:
            item: The bank statement line to match.
            counterparts: Available ledger transactions.
            matched_refs: Already-matched counterpart references to skip.
            config: Matching configuration.

        Returns:
            MatchResult if matched, None if no exact match found.
        """
        item_amount = _parse_amount(item.get("amount"))
        item_date = _parse_date(item.get("date"))
        item_ref = item.get("ref", "")
        item_reference = item.get("reference", "")

        if item_amount is None:
            return MatchResult(
                source_ref=item_ref,
                source_type=ReconciliationSourceType.BANK_STATEMENT_LINE,
                source_amount=Decimal("0.00"),
                match_status=MatchStatus.UNMATCHED,
                explanation="Bank statement line has no parseable amount.",
                requires_disposition=True,
            )

        for cp in counterparts:
            cp_ref = cp.get("ref", "")
            if cp_ref in matched_refs:
                continue

            cp_amount = _parse_amount(cp.get("amount"))
            if cp_amount is None:
                continue

            # Check exact amount match within tolerance
            amount_diff = abs(item_amount - cp_amount)
            if amount_diff > config.exact_amount_tolerance:
                continue

            # Check reference match
            cp_reference = cp.get("reference", "")
            if item_reference and cp_reference and item_reference == cp_reference:
                matched_refs.add(cp_ref)
                cp_date = _parse_date(cp.get("date"))
                return MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.BANK_STATEMENT_LINE,
                    source_amount=item_amount,
                    match_status=MatchStatus.MATCHED,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                            source_ref=cp_ref,
                            amount=cp_amount,
                            date=cp_date,
                            confidence=1.0,
                            match_reason="Exact amount and reference match.",
                        )
                    ],
                    difference_amount=amount_diff,
                    confidence=1.0,
                    explanation=f"Matched ledger {cp_ref!r} by amount {item_amount} and reference.",
                    requires_disposition=False,
                )

            # Check amount + date match
            cp_date = _parse_date(cp.get("date"))
            if item_date and cp_date:
                date_diff = abs((item_date - cp_date).days)
                if date_diff <= config.date_tolerance_days:
                    matched_refs.add(cp_ref)
                    return MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.BANK_STATEMENT_LINE,
                        source_amount=item_amount,
                        match_status=MatchStatus.MATCHED,
                        counterparts=[
                            MatchCounterpart(
                                source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                                source_ref=cp_ref,
                                amount=cp_amount,
                                date=cp_date,
                                confidence=0.95,
                                match_reason="Exact amount and date within tolerance.",
                            )
                        ],
                        difference_amount=amount_diff,
                        confidence=0.95,
                        explanation=f"Matched ledger {cp_ref!r} by amount {item_amount} and date proximity ({date_diff} days).",
                        requires_disposition=False,
                    )

        return None

    def _try_fuzzy_match(
        self,
        item: dict[str, Any],
        counterparts: list[dict[str, Any]],
        matched_refs: set[str],
        config: MatchingConfig,
    ) -> MatchResult:
        """Attempt fuzzy matching of one bank statement line with tolerance bands.

        Args:
            item: The bank statement line to match.
            counterparts: Available ledger transactions.
            matched_refs: Already-matched counterpart references to skip.
            config: Matching configuration.

        Returns:
            MatchResult with best fuzzy match or unmatched status.
        """
        item_amount = _parse_amount(item.get("amount"))
        item_date = _parse_date(item.get("date"))
        item_ref = item.get("ref", "")
        item_reference = item.get("reference", "")

        if item_amount is None:
            return MatchResult(
                source_ref=item_ref,
                source_type=ReconciliationSourceType.BANK_STATEMENT_LINE,
                source_amount=Decimal("0.00"),
                match_status=MatchStatus.UNMATCHED,
                explanation="Bank statement line has no parseable amount.",
                requires_disposition=True,
            )

        best_result: MatchResult | None = None
        best_confidence = 0.0

        for cp in counterparts:
            cp_ref = cp.get("ref", "")
            if cp_ref in matched_refs:
                continue

            cp_amount = _parse_amount(cp.get("amount"))
            if cp_amount is None:
                continue

            amount_diff = abs(item_amount - cp_amount)
            amount_confidence = _compute_amount_confidence(amount_diff, item_amount)

            # Compute date confidence
            cp_date = _parse_date(cp.get("date"))
            date_confidence = 0.0
            date_diff_days = 0
            if item_date and cp_date:
                date_diff_days = abs((item_date - cp_date).days)
                date_confidence = _compute_date_confidence(date_diff_days, config.date_tolerance_days)

            # Compute reference confidence
            cp_reference = cp.get("reference", "")
            ref_confidence = 0.0
            if item_reference and cp_reference:
                ref_confidence = _reference_similarity(item_reference, cp_reference)

            # Composite confidence: weighted average
            composite = (amount_confidence * 0.5) + (date_confidence * 0.3) + (ref_confidence * 0.2)

            if composite <= best_confidence:
                continue

            # Determine match status based on confidence
            if composite >= config.confidence_high:
                match_status = MatchStatus.MATCHED
            elif composite >= config.confidence_low:
                match_status = MatchStatus.PARTIALLY_MATCHED
            elif amount_diff <= item_amount * Decimal(str(config.fuzzy_amount_tolerance_pct)) / 100:
                match_status = MatchStatus.EXCEPTION
            else:
                continue

            best_confidence = composite
            best_result = MatchResult(
                source_ref=item_ref,
                source_type=ReconciliationSourceType.BANK_STATEMENT_LINE,
                source_amount=item_amount,
                match_status=match_status,
                counterparts=[
                    MatchCounterpart(
                        source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                        source_ref=cp_ref,
                        amount=cp_amount,
                        date=cp_date,
                        confidence=composite,
                        match_reason=f"Fuzzy match: amount_diff={amount_diff}, date_diff={date_diff_days}d, ref_sim={ref_confidence:.2f}",
                    )
                ],
                difference_amount=amount_diff,
                confidence=composite,
                explanation=f"Fuzzy matched ledger {cp_ref!r} (confidence={composite:.2f}).",
                requires_disposition=match_status in (MatchStatus.PARTIALLY_MATCHED, MatchStatus.EXCEPTION),
            )

        if best_result is None:
            return MatchResult(
                source_ref=item_ref,
                source_type=ReconciliationSourceType.BANK_STATEMENT_LINE,
                source_amount=item_amount,
                match_status=MatchStatus.UNMATCHED,
                counterparts=[],
                difference_amount=item_amount,
                confidence=0.0,
                explanation=f"No matching ledger transaction found for bank line {item_ref!r} ({item_amount}).",
                requires_disposition=True,
            )

        # Mark only the selected best counterpart as matched
        if best_result.match_status == MatchStatus.MATCHED and best_result.counterparts:
            matched_refs.add(best_result.counterparts[0].source_ref)

        return best_result


# ---------------------------------------------------------------------------
# AR/AP ageing matcher
# ---------------------------------------------------------------------------


class AgeingMatcher:
    """Match AR/AP ageing balances against open invoices and payments.

    This matcher groups outstanding receivables/payables by ageing buckets
    (current, 1-30, 31-60, 61-90, 90+) and matches them against ledger balances.
    """

    reconciliation_type = ReconciliationType.AR_AGEING  # Also used for AP_AGEING

    AGEING_BUCKETS = [
        ("current", 0),
        ("1-30", 30),
        ("31-60", 60),
        ("61-90", 90),
        ("90+", 91),
    ]

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match ageing balances against ledger open items.

        Args:
            source_items: Ageing bucket item dicts with keys: ref, amount, due_date, bucket.
            counterparts: Ledger open item dicts with keys: ref, amount, due_date, account_code.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []
        as_of_date = date.today()  # Can be overridden via config metadata

        for item in source_items:
            item_ref = item.get("ref", "")
            item_amount = _parse_amount(item.get("amount"))
            if item_amount is None:
                continue

            # Find matching ledger item by reference
            matched_cp = None
            for cp in counterparts:
                if cp.get("ref") == item_ref:
                    matched_cp = cp
                    break

            if matched_cp is None:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=item_amount,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"Ageing item {item_ref!r} not found in ledger open items.",
                        requires_disposition=True,
                        metadata={"bucket": item.get("bucket", "unknown")},
                    )
                )
                continue

            cp_amount = _parse_amount(matched_cp.get("amount"))
            cp_due_date = _parse_date(matched_cp.get("due_date"))
            amount_diff = abs(item_amount - cp_amount) if cp_amount else item_amount

            if amount_diff <= effective_config.exact_amount_tolerance:
                match_status = MatchStatus.MATCHED
                confidence = 1.0
            else:
                match_status = MatchStatus.EXCEPTION
                confidence = _compute_amount_confidence(amount_diff, item_amount)

            # Compute ageing bucket from due date
            age_days = 0
            bucket = "unknown"
            if cp_due_date:
                age_days = (as_of_date - cp_due_date).days
                for bucket_name, bucket_start in self.AGEING_BUCKETS:
                    if age_days < bucket_start or bucket_name == "90+":
                        bucket = bucket_name
                        break

            results.append(
                MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                    source_amount=item_amount,
                    match_status=match_status,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                            source_ref=matched_cp.get("ref", ""),
                            amount=cp_amount,
                            date=cp_due_date,
                            confidence=confidence,
                            match_reason="Amount match with ageing bucket classification.",
                        )
                    ],
                    difference_amount=amount_diff,
                    confidence=confidence,
                    explanation=f"Ageing item {item_ref!r} matched: bucket={bucket}, age={age_days}d, diff={amount_diff}",
                    requires_disposition=match_status == MatchStatus.EXCEPTION,
                    metadata={
                        "bucket": bucket,
                        "age_days": age_days,
                        "account_code": matched_cp.get("account_code"),
                    },
                )
            )

        return results


# ---------------------------------------------------------------------------
# Intercompany balance matcher
# ---------------------------------------------------------------------------


class IntercompanyMatcher:
    """Match intercompany balances between related entities.

    Compares balances reported by one entity against the corresponding
    counter-entity balances, flagging mismatches and timing differences.
    """

    reconciliation_type = ReconciliationType.INTERCOMPANY

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match intercompany balances across entities.

        Args:
            source_items: Balance dicts from entity A with keys: ref, amount, account_code, counter_entity.
            counterparts: Balance dicts from entity B with matching keys.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []

        for item in source_items:
            item_ref = item.get("ref", "")
            item_amount = _parse_amount(item.get("amount"))
            item_account = item.get("account_code", "")
            item_counter_entity = item.get("counter_entity", "")

            if item_amount is None:
                continue

            # Find counter-entity balance (should be equal and opposite)
            matched_cp = None
            for cp in counterparts:
                if (
                    cp.get("counter_entity") == item.get("entity")
                    and cp.get("account_code") == item_account
                ):
                    matched_cp = cp
                    break

            if matched_cp is None:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=item_amount,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"No counter-entity balance found for {item_ref!r} ({item_account}).",
                        requires_disposition=True,
                        metadata={"counter_entity": item_counter_entity, "account_code": item_account},
                    )
                )
                continue

            cp_amount = _parse_amount(matched_cp.get("amount"))
            if cp_amount is None:
                continue

            # Intercompany balances should net to zero (one debit, one credit)
            net_amount = item_amount + cp_amount  # One should be negative
            amount_diff = abs(net_amount)

            if amount_diff <= effective_config.exact_amount_tolerance:
                match_status = MatchStatus.MATCHED
                confidence = 1.0
                explanation = f"Intercompany balances net to zero for {item_ref!r}."
            elif amount_diff <= Decimal("1.00"):
                match_status = MatchStatus.PARTIALLY_MATCHED
                confidence = 0.8
                explanation = f"Intercompany balances have minor rounding difference ({amount_diff}) for {item_ref!r}."
            else:
                match_status = MatchStatus.EXCEPTION
                confidence = _compute_amount_confidence(amount_diff, abs(item_amount))
                explanation = (
                    f"Intercompany mismatch for {item_ref!r}: entity A={item_amount}, "
                    f"entity B={cp_amount}, net difference={amount_diff}."
                )

            results.append(
                MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                    source_amount=item_amount,
                    match_status=match_status,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                            source_ref=matched_cp.get("ref", ""),
                            amount=cp_amount,
                            confidence=confidence,
                            match_reason="Counter-entity balance comparison.",
                        )
                    ],
                    difference_amount=amount_diff,
                    confidence=confidence,
                    explanation=explanation,
                    requires_disposition=match_status in (MatchStatus.PARTIALLY_MATCHED, MatchStatus.EXCEPTION),
                    metadata={"counter_entity": item_counter_entity, "account_code": item_account},
                )
            )

        return results


# ---------------------------------------------------------------------------
# Payroll control matcher
# ---------------------------------------------------------------------------


class PayrollControlMatcher:
    """Match payroll control totals against payslip extractions and ledger entries.

    Verifies that payroll gross, deductions, net pay, and statutory contributions
    reconcile between source payslips, control accounts, and ledger postings.
    """

    reconciliation_type = ReconciliationType.PAYROLL_CONTROL

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match payroll control totals.

        Args:
            source_items: Payroll control total dicts with keys: category, amount, period.
            counterparts: Ledger posting or payslip total dicts with matching keys.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []

        # Group counterparts by category
        cp_by_category: dict[str, list[dict[str, Any]]] = {}
        for cp in counterparts:
            cat = cp.get("category", "unknown")
            cp_by_category.setdefault(cat, []).append(cp)

        for item in source_items:
            item_ref = item.get("ref", f"{item.get('category', 'unknown')}:{item.get('period', '')}")
            item_category = item.get("category", "")
            item_amount = _parse_amount(item.get("amount"))

            if item_amount is None:
                continue

            # Find counterpart by category
            cp_candidates = cp_by_category.get(item_category, [])
            if not cp_candidates:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=item_amount,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"No ledger/payslip total found for payroll category {item_category!r}.",
                        requires_disposition=True,
                        metadata={"category": item_category},
                    )
                )
                continue

            best_match: MatchResult | None = None
            best_diff = Decimal("999999999")

            for cp in cp_candidates:
                cp_amount = _parse_amount(cp.get("amount"))
                if cp_amount is None:
                    continue

                diff = abs(item_amount - cp_amount)
                if diff < best_diff:
                    best_diff = diff
                    if diff <= effective_config.exact_amount_tolerance:
                        best_match = MatchResult(
                            source_ref=item_ref,
                            source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                            source_amount=item_amount,
                            match_status=MatchStatus.MATCHED,
                            counterparts=[
                                MatchCounterpart(
                                    source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                                    source_ref=cp.get("ref", ""),
                                    amount=cp_amount,
                                    confidence=1.0,
                                    match_reason=f"Exact match for {item_category}.",
                                )
                            ],
                            difference_amount=diff,
                            confidence=1.0,
                            explanation=f"Payroll {item_category} matches ledger exactly.",
                            requires_disposition=False,
                        )
                    else:
                        confidence = _compute_amount_confidence(diff, item_amount)
                        best_match = MatchResult(
                            source_ref=item_ref,
                            source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                            source_amount=item_amount,
                            match_status=MatchStatus.EXCEPTION if confidence < 0.8 else MatchStatus.PARTIALLY_MATCHED,
                            counterparts=[
                                MatchCounterpart(
                                    source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                                    source_ref=cp.get("ref", ""),
                                    amount=cp_amount,
                                    confidence=confidence,
                                    match_reason=f"Partial match for {item_category}, diff={diff}.",
                                )
                            ],
                            difference_amount=diff,
                            confidence=confidence,
                            explanation=f"Payroll {item_category} differs by {diff}.",
                            requires_disposition=True,
                        )

            if best_match is None:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=item_amount,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"No parseable ledger amount for payroll category {item_category!r}.",
                        requires_disposition=True,
                        metadata={"category": item_category},
                    )
                )
            else:
                results.append(best_match)

        return results


# ---------------------------------------------------------------------------
# Fixed asset register matcher
# ---------------------------------------------------------------------------


class FixedAssetMatcher:
    """Match fixed asset register entries against ledger PPE balances and depreciation.

    Verifies asset cost, accumulated depreciation, net book value, disposals,
    and additions reconcile between the fixed asset register and the general ledger.
    """

    reconciliation_type = ReconciliationType.FIXED_ASSETS

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match fixed asset register entries.

        Args:
            source_items: Fixed asset register dicts with keys: asset_id, cost, accumulated_depreciation, net_book_value.
            counterparts: Ledger PPE account dicts with matching keys.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []

        for item in source_items:
            asset_id = item.get("asset_id", "")
            item_ref = f"asset:{asset_id}"
            cost = _parse_amount(item.get("cost"))
            acc_dep = _parse_amount(item.get("accumulated_depreciation"))
            nbv = _parse_amount(item.get("net_book_value"))

            if cost is None:
                continue

            # Find counterpart in ledger by asset reference
            matched_cp = None
            for cp in counterparts:
                if cp.get("asset_id") == asset_id:
                    matched_cp = cp
                    break

            if matched_cp is None:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=cost,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"Fixed asset {asset_id!r} not found in ledger.",
                        requires_disposition=True,
                        metadata={"asset_id": asset_id},
                    )
                )
                continue

            cp_cost = _parse_amount(matched_cp.get("cost"))
            cp_acc_dep = _parse_amount(matched_cp.get("accumulated_depreciation"))

            cost_diff = abs(cost - cp_cost) if cp_cost else cost
            dep_diff = abs((acc_dep or Decimal("0")) - (cp_acc_dep or Decimal("0")))

            total_diff = cost_diff + dep_diff

            if total_diff <= effective_config.exact_amount_tolerance:
                match_status = MatchStatus.MATCHED
                confidence = 1.0
            else:
                confidence = _compute_amount_confidence(total_diff, cost)
                match_status = MatchStatus.EXCEPTION if confidence < 0.7 else MatchStatus.PARTIALLY_MATCHED

            results.append(
                MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                    source_amount=cost,
                    match_status=match_status,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                            source_ref=matched_cp.get("ref", ""),
                            amount=cp_cost,
                            confidence=confidence,
                            match_reason=f"Asset register vs ledger: cost_diff={cost_diff}, dep_diff={dep_diff}.",
                        )
                    ],
                    difference_amount=total_diff,
                    confidence=confidence,
                    explanation=f"Fixed asset {asset_id!r}: cost_diff={cost_diff}, depreciation_diff={dep_diff}.",
                    requires_disposition=match_status != MatchStatus.MATCHED,
                    metadata={
                        "asset_id": asset_id,
                        "register_nbv": str(nbv),
                        "ledger_nbv": str((cp_cost or Decimal("0")) - (cp_acc_dep or Decimal("0"))),
                    },
                )
            )

        return results


# ---------------------------------------------------------------------------
# Loan amortisation matcher
# ---------------------------------------------------------------------------


class LoanAmortisationMatcher:
    """Match loan amortisation schedules against ledger loan balances and payments.

    Verifies that the outstanding principal, accrued interest, and scheduled
    payments reconcile between the amortisation schedule and the general ledger.
    """

    reconciliation_type = ReconciliationType.LOAN_AMORTISATION

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match loan amortisation schedule entries.

        Args:
            source_items: Amortisation schedule dicts with keys: payment_no, principal, interest, balance, due_date.
            counterparts: Ledger loan transaction dicts with matching keys.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []

        for item in source_items:
            payment_no = item.get("payment_no", "")
            item_ref = f"loan_payment:{payment_no}"
            schedule_principal = _parse_amount(item.get("principal"))
            schedule_interest = _parse_amount(item.get("interest"))
            schedule_balance = _parse_amount(item.get("balance"))

            if schedule_principal is None:
                continue

            # Find matching ledger payment
            matched_cp = None
            for cp in counterparts:
                if str(cp.get("payment_no", "")) == str(payment_no):
                    matched_cp = cp
                    break

            if matched_cp is None:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=schedule_principal,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"Loan payment {payment_no!r} not found in ledger.",
                        requires_disposition=True,
                        metadata={"payment_no": payment_no},
                    )
                )
                continue

            cp_principal = _parse_amount(matched_cp.get("principal"))
            cp_interest = _parse_amount(matched_cp.get("interest"))
            cp_balance = _parse_amount(matched_cp.get("balance"))

            principal_diff = abs(schedule_principal - (cp_principal or Decimal("0")))
            interest_diff = abs((schedule_interest or Decimal("0")) - (cp_interest or Decimal("0")))
            balance_diff = abs((schedule_balance or Decimal("0")) - (cp_balance or Decimal("0")))

            total_diff = principal_diff + interest_diff + balance_diff

            if total_diff <= effective_config.exact_amount_tolerance:
                match_status = MatchStatus.MATCHED
                confidence = 1.0
            else:
                baseline = schedule_principal + (schedule_interest or Decimal("0"))
                confidence = _compute_amount_confidence(total_diff, baseline if baseline else Decimal("1"))
                match_status = MatchStatus.EXCEPTION if confidence < 0.7 else MatchStatus.PARTIALLY_MATCHED

            results.append(
                MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                    source_amount=schedule_principal,
                    match_status=match_status,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                            source_ref=matched_cp.get("ref", ""),
                            amount=cp_principal,
                            confidence=confidence,
                            match_reason=f"Loan payment match: principal_diff={principal_diff}, interest_diff={interest_diff}.",
                        )
                    ],
                    difference_amount=total_diff,
                    confidence=confidence,
                    explanation=f"Loan payment {payment_no!r}: total_diff={total_diff}.",
                    requires_disposition=match_status != MatchStatus.MATCHED,
                    metadata={"payment_no": payment_no},
                )
            )

        return results


# ---------------------------------------------------------------------------
# Accrual tracker matcher
# ---------------------------------------------------------------------------


class AccrualTrackerMatcher:
    """Match accrued income and expense tracking against expected obligations.

    Compares accrued amounts in the ledger against expected accruals computed
    from contracts, invoices, and policy rules.
    """

    reconciliation_type = ReconciliationType.ACCRUAL_TRACKER

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match accrual tracking entries.

        Args:
            source_items: Expected accrual dicts with keys: ref, amount, account_code, period.
            counterparts: Ledger accrual entry dicts with matching keys.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []

        # Index counterparts by account_code + period
        cp_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for cp in counterparts:
            key = (cp.get("account_code", ""), cp.get("period", ""))
            cp_index.setdefault(key, []).append(cp)

        for item in source_items:
            item_ref = item.get("ref", "")
            item_amount = _parse_amount(item.get("amount"))
            item_account = item.get("account_code", "")
            item_period = item.get("period", "")

            if item_amount is None:
                continue

            cp_candidates = cp_index.get((item_account, item_period), [])
            if not cp_candidates:
                results.append(
                    MatchResult(
                        source_ref=item_ref,
                        source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                        source_amount=item_amount,
                        match_status=MatchStatus.UNMATCHED,
                        explanation=f"No ledger accrual found for {item_account} in period {item_period}.",
                        requires_disposition=True,
                        metadata={"account_code": item_account, "period": item_period},
                    )
                )
                continue

            # Sum all counterpart amounts for this account+period
            total_cp_amount = sum(
                (_parse_amount(cp.get("amount")) or Decimal("0")) for cp in cp_candidates
            )
            diff = abs(item_amount - total_cp_amount)

            if diff <= effective_config.exact_amount_tolerance:
                match_status = MatchStatus.MATCHED
                confidence = 1.0
            else:
                confidence = _compute_amount_confidence(diff, item_amount)
                match_status = MatchStatus.EXCEPTION if confidence < 0.7 else MatchStatus.PARTIALLY_MATCHED

            results.append(
                MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                    source_amount=item_amount,
                    match_status=match_status,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                            source_ref=cp.get("ref", ""),
                            amount=_parse_amount(cp.get("amount")),
                            confidence=confidence,
                            match_reason=f"Accrual comparison for {item_account}:{item_period}.",
                        )
                        for cp in cp_candidates
                    ],
                    difference_amount=diff,
                    confidence=confidence,
                    explanation=f"Accrual for {item_ref!r}: expected={item_amount}, ledger={total_cp_amount}, diff={diff}.",
                    requires_disposition=match_status != MatchStatus.MATCHED,
                    metadata={"account_code": item_account, "period": item_period},
                )
            )

        return results


# ---------------------------------------------------------------------------
# Budget vs actual matcher
# ---------------------------------------------------------------------------


class BudgetVsActualMatcher:
    """Match budgeted amounts against actual ledger postings.

    Computes variance (actual - budget) and flags items exceeding configured
    variance thresholds for reviewer investigation.
    """

    reconciliation_type = ReconciliationType.BUDGET_VS_ACTUAL

    def match(
        self,
        source_items: list[dict[str, Any]],
        counterparts: list[dict[str, Any]],
        config: MatchingConfig | None = None,
    ) -> list[MatchResult]:
        """Match budget vs actual entries.

        Args:
            source_items: Budget line dicts with keys: account_code, budget_amount, period.
            counterparts: Ledger posting dicts with keys: account_code, amount, period.
            config: Optional matching configuration.

        Returns:
            List of MatchResult objects.
        """
        effective_config = config or DEFAULT_MATCHING_CONFIG
        results: list[MatchResult] = []

        # Index counterparts by account_code + period + dimensional split.
        cp_index: dict[tuple[str, str, str, str, str], Decimal] = {}
        cp_ref_by_key: dict[tuple[str, str, str, str, str], str] = {}
        for cp in counterparts:
            key = _budget_dimension_key(cp)
            amount = _parse_amount(cp.get("amount")) or Decimal("0")
            cp_index[key] = cp_index.get(key, Decimal("0")) + amount
            cp_ref_by_key.setdefault(
                key,
                str(
                    cp.get("ref")
                    or _build_budget_reference(
                        prefix="ledger:budget",
                        account_code=key[0],
                        period=key[1],
                        department=key[2],
                        cost_centre=key[3],
                        project=key[4],
                    )
                ),
            )

        for item in source_items:
            account_code, period, department, cost_centre, project = _budget_dimension_key(item)
            item_ref = _build_budget_reference(
                prefix="budget",
                account_code=account_code,
                period=period,
                department=department,
                cost_centre=cost_centre,
                project=project,
            )
            budget_amount = _parse_amount(item.get("budget_amount"))

            if budget_amount is None:
                continue

            actual_amount = cp_index.get(
                (account_code, period, department, cost_centre, project),
                Decimal("0"),
            )
            variance = actual_amount - budget_amount

            # Budget vs actual is informational — we report variance, not match
            if budget_amount == 0:
                # Zero budget: only zero actuals are acceptable; any spend is unbudgeted
                if actual_amount == 0:
                    match_status = MatchStatus.MATCHED
                    variance_pct = Decimal("0")
                    confidence = 1.0
                else:
                    match_status = MatchStatus.EXCEPTION
                    variance_pct = Decimal("100")
                    confidence = 0.0
            else:
                variance_pct = abs(variance / budget_amount * 100)
                if variance_pct <= Decimal(str(effective_config.fuzzy_amount_tolerance_pct)):
                    match_status = MatchStatus.MATCHED
                    confidence = 1.0
                elif variance_pct <= Decimal("10"):
                    match_status = MatchStatus.PARTIALLY_MATCHED
                    confidence = 0.7
                else:
                    match_status = MatchStatus.EXCEPTION
                    confidence = 0.3

            results.append(
                MatchResult(
                    source_ref=item_ref,
                    source_type=ReconciliationSourceType.EXTERNAL_BALANCE,
                    source_amount=budget_amount,
                    match_status=match_status,
                    counterparts=[
                        MatchCounterpart(
                            source_type=ReconciliationSourceType.LEDGER_TRANSACTION,
                            source_ref=cp_ref_by_key.get(
                                (account_code, period, department, cost_centre, project),
                                _build_budget_reference(
                                    prefix="ledger:budget",
                                    account_code=account_code,
                                    period=period,
                                    department=department,
                                    cost_centre=cost_centre,
                                    project=project,
                                ),
                            ),
                            amount=actual_amount,
                            confidence=confidence,
                            match_reason=f"Budget vs actual: variance={variance} ({variance_pct:.1f}%)",
                        )
                    ],
                    difference_amount=variance,
                    confidence=confidence,
                    explanation=f"Budget vs actual for {account_code}:{period}: budget={budget_amount}, actual={actual_amount}, variance={variance} ({variance_pct:.1f}%)",
                    requires_disposition=match_status == MatchStatus.EXCEPTION,
                    metadata={
                        "account_code": account_code,
                        "period": period,
                        **({"department": department} if department else {}),
                        **({"cost_centre": cost_centre} if cost_centre else {}),
                        **({"project": project} if project else {}),
                        "variance_pct": float(variance_pct),
                    },
                )
            )

        return results


# ---------------------------------------------------------------------------
# Trial balance checker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialBalanceAnomaly:
    """Represent one anomaly detected during trial balance validation.

    Attributes:
        anomaly_type: Category of the anomaly.
        severity: Severity level (info, warning, blocking).
        account_code: Associated GL account code, if applicable.
        description: Human-readable description.
        details: Structured anomaly details.
    """

    anomaly_type: AnomalyType
    severity: str
    account_code: str | None
    description: str
    details: JsonObject


class TrialBalanceChecker:
    """Validate trial balance for debit/credit equality, unusual balances, and anomalies.

    This checker runs deterministic checks against computed trial balance data:
    1. Debit-equals-credit verification (blocking if fails).
    2. Unusual balance direction detection (e.g., credit balance on asset account).
    3. Zero-balance detection on normally active accounts.
    4. Missing account detection from the chart of accounts.
    5. Rounding difference detection for near-balanced trials.
    """

    # Account types that normally have debit balances
    NORMAL_DEBIT_TYPES: ClassVar[set[AccountType]] = {
        AccountType.ASSET, AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE,
    }
    # Account types that normally have credit balances
    NORMAL_CREDIT_TYPES: ClassVar[set[AccountType]] = {
        AccountType.LIABILITY, AccountType.EQUITY, AccountType.REVENUE, AccountType.OTHER_INCOME,
    }

    def check_balance(
        self,
        account_balances: list[dict[str, Any]],
        rounding_tolerance: Decimal = Decimal("0.02"),
    ) -> tuple[bool, Decimal, Decimal, list[TrialBalanceAnomaly]]:
        """Check that total debits equal total credits and detect anomalies.

        Args:
            account_balances: List of account balance dicts with keys:
                account_code, account_name, account_type, debit_balance, credit_balance.
            rounding_tolerance: Maximum acceptable imbalance for rounding differences.

        Returns:
            Tuple of (is_balanced, total_debits, total_credits, anomalies).
        """
        total_debits = Decimal("0.00")
        total_credits = Decimal("0.00")
        anomalies: list[TrialBalanceAnomaly] = []

        for acct in account_balances:
            debit = _parse_amount(acct.get("debit_balance")) or Decimal("0.00")
            credit = _parse_amount(acct.get("credit_balance")) or Decimal("0.00")
            total_debits += debit
            total_credits += credit

        imbalance = total_debits - total_credits
        is_balanced = abs(imbalance) <= rounding_tolerance

        if abs(imbalance) > 0 and abs(imbalance) <= rounding_tolerance:
            # Non-zero but within tolerance — acceptable rounding difference
            anomalies.append(
                TrialBalanceAnomaly(
                    anomaly_type=AnomalyType.ROUNDING_DIFFERENCE,
                    severity="info",
                    account_code=None,
                    description=f"Trial balance has a rounding difference of {imbalance}.",
                    details={
                        "imbalance": str(imbalance),
                        "tolerance": str(rounding_tolerance),
                    },
                )
            )
        elif not is_balanced:
            # Beyond tolerance — blocking imbalance
            anomalies.append(
                TrialBalanceAnomaly(
                    anomaly_type=AnomalyType.DEBIT_CREDIT_IMBALANCE,
                    severity="blocking",
                    account_code=None,
                    description=f"Trial balance is imbalanced: debits={total_debits}, credits={total_credits}, difference={imbalance}.",
                    details={
                        "total_debits": str(total_debits),
                        "total_credits": str(total_credits),
                        "imbalance": str(imbalance),
                    },
                )
            )

        return is_balanced, total_debits, total_credits, anomalies

    def check_unusual_balances(
        self,
        account_balances: list[dict[str, Any]],
    ) -> list[TrialBalanceAnomaly]:
        """Detect accounts with balance directions unexpected for their account type.

        Args:
            account_balances: List of account balance dicts.

        Returns:
            List of TrialBalanceAnomaly objects for unusual balances.
        """
        anomalies: list[TrialBalanceAnomaly] = []

        for acct in account_balances:
            account_code = acct.get("account_code", "")
            account_type = acct.get("account_type", "")
            debit = _parse_amount(acct.get("debit_balance")) or Decimal("0.00")
            credit = _parse_amount(acct.get("credit_balance")) or Decimal("0.00")
            net = debit - credit

            # Check for unusual balance direction
            if account_type in self.NORMAL_DEBIT_TYPES and net < 0:
                anomalies.append(
                    TrialBalanceAnomaly(
                        anomaly_type=AnomalyType.UNUSUAL_ACCOUNT_BALANCE,
                        severity="warning",
                        account_code=account_code,
                        description=f"Account {account_code} ({acct.get('account_name', '')}) has a credit balance of {abs(net)} but is a {account_type} account (normally debit).",
                        details={
                            "account_type": account_type,
                            "net_balance": str(net),
                            "expected_direction": "debit",
                        },
                    )
                )
            elif account_type in self.NORMAL_CREDIT_TYPES and net > 0:
                anomalies.append(
                    TrialBalanceAnomaly(
                        anomaly_type=AnomalyType.UNUSUAL_ACCOUNT_BALANCE,
                        severity="warning",
                        account_code=account_code,
                        description=f"Account {account_code} ({acct.get('account_name', '')}) has a debit balance of {net} but is a {account_type} account (normally credit).",
                        details={
                            "account_type": account_type,
                            "net_balance": str(net),
                            "expected_direction": "credit",
                        },
                    )
                )

            # Check for zero balance on potentially active accounts
            if net == 0 and debit == 0 and credit == 0 and acct.get("is_active", False):
                anomalies.append(
                    TrialBalanceAnomaly(
                        anomaly_type=AnomalyType.ZERO_BALANCE_ACTIVE,
                        severity="info",
                        account_code=account_code,
                        description=f"Account {account_code} ({acct.get('account_name', '')}) has zero balance but is marked active.",
                        details={
                            "account_type": account_type,
                        },
                    )
                )

        return anomalies

    def check_missing_accounts(
        self,
        account_balances: list[dict[str, Any]],
        expected_account_codes: set[str],
    ) -> list[TrialBalanceAnomaly]:
        """Detect expected accounts that are missing from the trial balance.

        Args:
            account_balances: List of account balance dicts.
            expected_account_codes: Set of account codes that should appear.

        Returns:
            List of TrialBalanceAnomaly objects for missing accounts.
        """
        anomalies: list[TrialBalanceAnomaly] = []
        present_codes = {acct.get("account_code", "") for acct in account_balances}

        for expected_code in expected_account_codes:
            if expected_code not in present_codes:
                anomalies.append(
                    TrialBalanceAnomaly(
                        anomaly_type=AnomalyType.MISSING_ACCOUNT,
                        severity="warning",
                        account_code=expected_code,
                        description=f"Expected account {expected_code} is missing from the trial balance.",
                        details={"expected_account_code": expected_code},
                    )
                )

        return anomalies

    def check_variance(
        self,
        current_balances: list[dict[str, Any]],
        prior_balances: list[dict[str, Any]],
        variance_threshold_pct: float = 20.0,
    ) -> list[TrialBalanceAnomaly]:
        """Detect unexplained month-over-month variances exceeding a threshold.

        Args:
            current_balances: Current period account balance dicts.
            prior_balances: Prior period account balance dicts.
            variance_threshold_pct: Percentage threshold for flagging variances.

        Returns:
            List of TrialBalanceAnomaly objects for unexplained variances.
        """
        anomalies: list[TrialBalanceAnomaly] = []
        current_index = {acct["account_code"]: acct for acct in current_balances}
        prior_index = {acct["account_code"]: acct for acct in prior_balances}

        all_codes = set(current_index.keys()) | set(prior_index.keys())

        for code in all_codes:
            current = current_index.get(code)
            prior = prior_index.get(code)

            current_net = (
                (_parse_amount(current.get("debit_balance")) or Decimal("0"))
                - (_parse_amount(current.get("credit_balance")) or Decimal("0"))
                if current
                else Decimal("0")
            )
            prior_net = (
                (_parse_amount(prior.get("debit_balance")) or Decimal("0"))
                - (_parse_amount(prior.get("credit_balance")) or Decimal("0"))
                if prior
                else Decimal("0")
            )

            if prior_net == 0:
                if current_net != 0:
                    anomalies.append(
                        TrialBalanceAnomaly(
                            anomaly_type=AnomalyType.UNEXPLAINED_VARIANCE,
                            severity="warning",
                            account_code=code,
                            description=f"Account {code} had zero balance last period but now has {current_net}.",
                            details={
                                "prior_balance": "0",
                                "current_balance": str(current_net),
                                "variance_pct": float("inf"),
                            },
                        )
                    )
                continue

            variance = abs(current_net - prior_net)
            variance_pct = abs(variance / prior_net * 100)

            if variance_pct > variance_threshold_pct:
                anomalies.append(
                    TrialBalanceAnomaly(
                        anomaly_type=AnomalyType.UNEXPLAINED_VARIANCE,
                        severity="warning",
                        account_code=code,
                        description=f"Account {code} has a {variance_pct:.1f}% MoM variance (threshold: {variance_threshold_pct}%).",
                        details={
                            "prior_balance": str(prior_net),
                            "current_balance": str(current_net),
                            "variance": str(variance),
                            "variance_pct": float(variance_pct),
                            "threshold_pct": float(variance_threshold_pct),
                        },
                    )
                )

        return anomalies


# ---------------------------------------------------------------------------
# Matcher registry
# ---------------------------------------------------------------------------

#: Mapping from reconciliation type to the canonical matcher class.
MATCHER_REGISTRY: dict[ReconciliationType, type[MatcherProtocol]] = {
    ReconciliationType.BANK_RECONCILIATION: BankReconciliationMatcher,
    ReconciliationType.AR_AGEING: AgeingMatcher,
    ReconciliationType.AP_AGEING: AgeingMatcher,
    ReconciliationType.INTERCOMPANY: IntercompanyMatcher,
    ReconciliationType.PAYROLL_CONTROL: PayrollControlMatcher,
    ReconciliationType.FIXED_ASSETS: FixedAssetMatcher,
    ReconciliationType.LOAN_AMORTISATION: LoanAmortisationMatcher,
    ReconciliationType.ACCRUAL_TRACKER: AccrualTrackerMatcher,
    ReconciliationType.BUDGET_VS_ACTUAL: BudgetVsActualMatcher,
}

__all__ = [
    "DEFAULT_MATCHING_CONFIG",
    "MATCHER_REGISTRY",
    "AccrualTrackerMatcher",
    "AgeingMatcher",
    "BankReconciliationMatcher",
    "BudgetVsActualMatcher",
    "FixedAssetMatcher",
    "IntercompanyMatcher",
    "LoanAmortisationMatcher",
    "MatchCounterpart",
    "MatchResult",
    "MatcherProtocol",
    "MatchingConfig",
    "PayrollControlMatcher",
    "TrialBalanceAnomaly",
    "TrialBalanceChecker",
]
