"""
Purpose: Verify focused regressions in document quality-check orchestration.
Scope: Ensure transaction-mismatch blockers are only created once extraction-backed
matching has actually run.
Dependencies: document_quality_checks orchestration and transaction-matching contracts.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from apps.worker.app.tasks import document_quality_checks as quality_checks_module
from services.documents.duplicate_detection import DuplicateDetectionResult
from services.documents.transaction_matching import (
    AutoTransactionMatchResult,
    AutoTransactionMatchStatus,
)


def test_quality_checks_skip_transaction_mismatch_until_extraction_exists(
    monkeypatch,
) -> None:
    """Missing extraction should not create a fake transaction-mismatch blocker."""

    created_issue_types: list[str] = []

    class _DuplicateService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def check_duplicate(self, **kwargs):
            del kwargs
            return DuplicateDetectionResult(
                is_duplicate=False,
                existing_document_id=None,
            )

        def refresh_close_run_duplicates(self, **kwargs):
            del kwargs
            return {}

    class _PeriodService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def validate_period(self, **kwargs):
            return SimpleNamespace(
                is_valid=True,
                document_period_start=kwargs["document_period_start"],
                document_period_end=kwargs["document_period_end"],
                close_run_period_start=kwargs["close_run_period_start"],
                close_run_period_end=kwargs["close_run_period_end"],
                validation_method="period_overlap_check",
            )

    class _CompletenessService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def check_completeness(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                is_complete=True,
                missing_document_types=set(),
                present_document_types=set(),
                required_document_types=set(),
            )

    class _IssueService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def create_issue(self, **kwargs):
            created_issue_types.append(kwargs["issue_type"])
            return SimpleNamespace(
                id=uuid4(),
                severity=kwargs["severity"],
            )

        def get_document_issues(self, **kwargs):
            del kwargs
            return []

    class _TransactionMatcher:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def evaluate_and_persist(self, **kwargs):
            del kwargs
            return AutoTransactionMatchResult(
                status=AutoTransactionMatchStatus.UNMATCHED,
                score=None,
                match_source=None,
                matched_document_id=None,
                matched_document_filename=None,
                matched_line_no=None,
                matched_reference=None,
                matched_description=None,
                matched_date=None,
                matched_amount=None,
                reasons=(
                    "Structured extraction is not available yet for transaction matching.",
                ),
                extraction_available=False,
            )

    monkeypatch.setattr(
        quality_checks_module,
        "DuplicateDetectionService",
        _DuplicateService,
    )
    monkeypatch.setattr(
        quality_checks_module,
        "PeriodValidationService",
        _PeriodService,
    )
    monkeypatch.setattr(
        quality_checks_module,
        "CompletenessCheckService",
        _CompletenessService,
    )
    monkeypatch.setattr(
        quality_checks_module,
        "DocumentIssueService",
        _IssueService,
    )
    monkeypatch.setattr(
        quality_checks_module,
        "TransactionMatchingService",
        _TransactionMatcher,
    )

    result = quality_checks_module.run_document_quality_checks(
        entity_id=uuid4(),
        close_run_id=uuid4(),
        document_id=uuid4(),
        document_hash="abc123",
        document_file_size=1024,
        document_period_start=None,
        document_period_end=None,
        close_run_period_start=SimpleNamespace(isoformat=lambda: "2026-03-01"),
        close_run_period_end=SimpleNamespace(isoformat=lambda: "2026-03-31"),
        actor_user_id=uuid4(),
        document_repo=SimpleNamespace(),
        entity_repo=SimpleNamespace(),
        storage_repo=SimpleNamespace(),
        db_session=SimpleNamespace(),
    )

    assert "transaction_mismatch" not in created_issue_types
    assert result["issues_created"] == []
    assert result["passed_all_checks"] is True
    assert result["transaction_match"]["status"] == "unmatched"


def test_quality_checks_create_duplicate_issue_when_exact_match_is_found(monkeypatch) -> None:
    """Duplicate detection should surface a blocking duplicate-document issue."""

    created_issue_types: list[str] = []
    created_issue_details: list[dict[str, object]] = []
    current_document_id = uuid4()
    existing_document_id = uuid4()

    class _DuplicateService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def check_duplicate(self, **kwargs):
            assert kwargs["current_document_id"] == str(current_document_id)
            return DuplicateDetectionResult(
                is_duplicate=True,
                existing_document_id=str(existing_document_id),
                existing_document_filename="existing.pdf",
                similarity_score=1.0,
                detection_method="sha256_exact",
                matched_fields=("sha256_hash",),
            )

        def refresh_close_run_duplicates(self, **kwargs):
            del kwargs
            document_id = current_document_id
            return {
                document_id: DuplicateDetectionResult(
                    is_duplicate=True,
                    existing_document_id=str(existing_document_id),
                    existing_document_filename="existing.pdf",
                    similarity_score=1.0,
                    detection_method="sha256_exact",
                    matched_fields=("sha256_hash",),
                )
            }

    class _PeriodService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def validate_period(self, **kwargs):
            return SimpleNamespace(
                is_valid=True,
                document_period_start=kwargs["document_period_start"],
                document_period_end=kwargs["document_period_end"],
                close_run_period_start=kwargs["close_run_period_start"],
                close_run_period_end=kwargs["close_run_period_end"],
                validation_method="period_overlap_check",
            )

    class _CompletenessService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def check_completeness(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                is_complete=True,
                missing_document_types=set(),
                present_document_types=set(),
                required_document_types=set(),
            )

    class _IssueService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def create_issue(self, **kwargs):
            created_issue_types.append(kwargs["issue_type"])
            created_issue_details.append(kwargs["details"])
            return SimpleNamespace(
                id=uuid4(),
                severity=kwargs["severity"],
            )

        def get_document_issues(self, **kwargs):
            del kwargs
            return []

    class _TransactionMatcher:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def evaluate_and_persist(self, **kwargs):
            del kwargs
            return AutoTransactionMatchResult(
                status=AutoTransactionMatchStatus.UNMATCHED,
                score=None,
                match_source=None,
                matched_document_id=None,
                matched_document_filename=None,
                matched_line_no=None,
                matched_reference=None,
                matched_description=None,
                matched_date=None,
                matched_amount=None,
                reasons=(),
                extraction_available=False,
            )

    monkeypatch.setattr(quality_checks_module, "DuplicateDetectionService", _DuplicateService)
    monkeypatch.setattr(quality_checks_module, "PeriodValidationService", _PeriodService)
    monkeypatch.setattr(quality_checks_module, "CompletenessCheckService", _CompletenessService)
    monkeypatch.setattr(quality_checks_module, "DocumentIssueService", _IssueService)
    monkeypatch.setattr(quality_checks_module, "TransactionMatchingService", _TransactionMatcher)

    result = quality_checks_module.run_document_quality_checks(
        entity_id=uuid4(),
        close_run_id=uuid4(),
        document_id=current_document_id,
        document_hash="abc123",
        document_file_size=1024,
        document_period_start=None,
        document_period_end=None,
        close_run_period_start=SimpleNamespace(isoformat=lambda: "2026-03-01"),
        close_run_period_end=SimpleNamespace(isoformat=lambda: "2026-03-31"),
        actor_user_id=uuid4(),
        document_repo=SimpleNamespace(),
        entity_repo=SimpleNamespace(),
        storage_repo=SimpleNamespace(),
        db_session=SimpleNamespace(),
    )

    assert created_issue_types == ["duplicate_document"]
    assert created_issue_details[0]["existing_document_filename"] == "existing.pdf"
    assert created_issue_details[0]["matched_fields"] == ["sha256_hash"]
    assert result["issues_created"][0]["issue_type"] == "duplicate_document"
    assert result["passed_all_checks"] is False
