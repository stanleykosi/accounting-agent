"""
Purpose: Verify checkpointed worker jobs support retries, cancellation, and resume flows.
Scope: Shared task wrapper behavior, retry scheduling metadata, cancel-aware execution,
and checkpoint carry-forward when resuming failed jobs.
Dependencies: Tracked worker task base, job retry policy, durable job record contract,
and lightweight in-memory doubles for job lifecycle storage.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any, TypeVar
from uuid import UUID, uuid4

import pytest
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from services.common.enums import JobStatus
from services.common.types import JsonObject
from services.jobs.retry_policy import JobCancellationRequestedError
from services.jobs.service import JobRecord, JobService
from services.jobs.task_names import TaskName, resolve_task_route

generate_reports_task = import_module("apps.worker.app.tasks.generate_reports")
parse_documents_task = import_module("apps.worker.app.tasks.parse_documents")


def test_tracked_job_wrapper_persists_checkpoint_and_schedules_retry() -> None:
    """Ensure retryable failures keep checkpoints and increment retry metadata."""

    controller = InMemoryJobController()
    job = controller.seed_job(task_name=TaskName.DOCUMENT_PARSE_AND_EXTRACT.value)
    task = FakeTrackedTask(controller=controller, job_id=job.id, retries=0, max_retries=5)

    with pytest.raises(RetryScheduledError):
        task.run_tracked_job(
            runner=lambda context: _run_retryable_runner(
                context=context,
                error=RuntimeError("temporary parse failure"),
            )
        )

    updated_job = controller.jobs[job.id]
    assert updated_job.status is JobStatus.QUEUED
    assert updated_job.retry_count == 1
    assert updated_job.failure_reason == "temporary parse failure"
    assert updated_job.checkpoint_payload["current_step"] == "download_source_document"


def test_tracked_job_wrapper_honors_operator_cancellation() -> None:
    """Ensure a cancel request stops execution and records a canceled terminal state."""

    controller = InMemoryJobController()
    job = controller.seed_job(
        task_name=TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK.value,
        cancellation_requested_at=datetime.now(tz=UTC),
    )
    task = FakeTrackedTask(controller=controller, job_id=job.id, retries=0, max_retries=4)

    result = task.run_tracked_job(
        runner=lambda context: _run_success_runner(context=context, step="load_report_context")
    )

    updated_job = controller.jobs[job.id]
    assert result["status"] == JobStatus.CANCELED.value
    assert updated_job.status is JobStatus.CANCELED
    assert updated_job.canceled_at is not None


def test_resume_job_carries_forward_checkpoint_payload() -> None:
    """Ensure resuming a failed job creates a fresh queued execution with copied checkpoints."""

    controller = InMemoryJobController()
    failed_job = controller.seed_job(
        task_name=TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN.value,
        status=JobStatus.FAILED,
        checkpoint_payload={
            "completed_steps": ["load_recommendation_context"],
            "current_step": "load_recommendation_context",
            "state": {"document_id": "doc-123"},
        },
    )

    resumed_job = controller.resume_job(
        job_id=failed_job.id,
        actor_user_id=UUID("10000000-0000-0000-0000-000000000001"),
        reason="Retry after model gateway recovered.",
    )

    assert resumed_job.status is JobStatus.QUEUED
    assert resumed_job.resumed_from_job_id == failed_job.id
    assert resumed_job.checkpoint_payload["state"] == {"document_id": "doc-123"}
    assert resumed_job.checkpoint_payload["resume_reason"] == "Retry after model gateway recovered."


def test_request_cancellation_uses_membership_scoped_lookup_and_clears_blocking_reason() -> None:
    """Ensure cancellation threads actor membership and clears blocking metadata."""

    actor_user_id = UUID("10000000-0000-0000-0000-000000000001")
    service = JobService(db_session=SimpleNamespace(commit=lambda: None))
    captured_lookup: dict[str, UUID] = {}
    job_row = _build_mutable_job_row(
        status=JobStatus.BLOCKED,
        blocking_reason="Waiting for manual recovery.",
    )

    def fake_load_job_for_update(*, job_id: UUID, entity_id: UUID, user_id: UUID) -> Any:
        captured_lookup["job_id"] = job_id
        captured_lookup["entity_id"] = entity_id
        captured_lookup["user_id"] = user_id
        return job_row

    service._load_job_for_update = fake_load_job_for_update  # type: ignore[method-assign]

    result = service.request_cancellation(
        entity_id=UUID("20000000-0000-0000-0000-000000000001"),
        job_id=job_row.id,
        actor_user_id=actor_user_id,
        reason="Operator canceled blocked execution.",
    )

    assert captured_lookup["user_id"] == actor_user_id
    assert result.status is JobStatus.CANCELED
    assert job_row.blocking_reason is None


def test_resume_job_uses_membership_scoped_lookup() -> None:
    """Ensure resume requests also scope the source-job lookup to the actor membership."""

    actor_user_id = UUID("10000000-0000-0000-0000-000000000001")
    service = JobService(db_session=SimpleNamespace())
    captured_lookup: dict[str, UUID] = {}
    source_job = _build_mutable_job_row(
        status=JobStatus.FAILED,
        checkpoint_payload={"completed_steps": ["build_excel_pack"]},
    )

    def fake_load_job_for_update(*, job_id: UUID, entity_id: UUID, user_id: UUID) -> Any:
        captured_lookup["job_id"] = job_id
        captured_lookup["entity_id"] = entity_id
        captured_lookup["user_id"] = user_id
        return source_job

    def fake_dispatch_job(**kwargs: Any) -> JobRecord:
        return JobRecord(
            id=uuid4(),
            entity_id=source_job.entity_id,
            close_run_id=source_job.close_run_id,
            document_id=source_job.document_id,
            actor_user_id=actor_user_id,
            canceled_by_user_id=None,
            resumed_from_job_id=source_job.id,
            task_name=source_job.task_name,
            queue_name="reporting",
            routing_key="reporting.generate_close_run_pack",
            status=JobStatus.QUEUED,
            payload=kwargs["payload"],
            checkpoint_payload=kwargs["checkpoint_payload"],
            result_payload=None,
            failure_reason=None,
            failure_details=None,
            blocking_reason=None,
            trace_id=None,
            attempt_count=0,
            retry_count=0,
            max_retries=4,
            started_at=None,
            completed_at=None,
            cancellation_requested_at=None,
            canceled_at=None,
            dead_lettered_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    service._load_job_for_update = fake_load_job_for_update  # type: ignore[method-assign]
    service.dispatch_job = fake_dispatch_job  # type: ignore[method-assign]

    result = service.resume_job(
        dispatcher=SimpleNamespace(),
        entity_id=UUID("20000000-0000-0000-0000-000000000001"),
        job_id=source_job.id,
        actor_user_id=actor_user_id,
        reason="Resume after retry budget reset.",
    )

    assert captured_lookup["user_id"] == actor_user_id
    assert result.checkpoint_payload["resume_reason"] == "Resume after retry budget reset."


def test_report_generation_re_raises_cancellation_after_marking_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure report-task cancellation escapes the catch-all and marks the run failed."""

    repo_holder: dict[str, FakeReportRepository] = {}

    monkeypatch.setattr(
        generate_reports_task,
        "get_session_factory",
        lambda: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "ReportRepository",
        lambda db_session: repo_holder.setdefault("repo", FakeReportRepository()),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_load_report_context",
        lambda **_: SimpleNamespace(
            entity_id=UUID("20000000-0000-0000-0000-000000000001"),
            template_id=UUID("30000000-0000-0000-0000-000000000001"),
            period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
            close_run_id=UUID("40000000-0000-0000-0000-000000000001"),
            entity_name="Acme",
            currency_code="NGN",
        ),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_gather_section_data",
        lambda **_: {"p_and_l": {}},
    )
    monkeypatch.setattr(generate_reports_task, "ensure_close_run_active_phase", lambda **_: None)

    job_context = FakeCheckpointContext(cancel_on_call=2)
    with pytest.raises(JobCancellationRequestedError):
        generate_reports_task._run_report_generation_task(
            close_run_id="40000000-0000-0000-0000-000000000001",
            report_run_id=None,
            actor_user_id="10000000-0000-0000-0000-000000000001",
            sections=None,
            generate_commentary_flag=False,
            use_llm_commentary=False,
            job_context=job_context,
        )

    assert repo_holder["repo"].status_updates[-1]["status"] == "failed"
    assert "canceled by an operator" in repo_holder["repo"].status_updates[-1]["failure_reason"]


def test_report_generation_marks_precreated_run_failed_when_phase_guard_cancels_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-guard cancellation should still retire a pre-created report run."""

    repo_holder: dict[str, FakeReportRepository] = {}

    monkeypatch.setattr(
        generate_reports_task,
        "get_session_factory",
        lambda: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "ReportRepository",
        lambda db_session: repo_holder.setdefault("repo", FakeReportRepository()),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_load_report_context",
        lambda **_: SimpleNamespace(
            entity_id=UUID("20000000-0000-0000-0000-000000000001"),
            template_id=UUID("30000000-0000-0000-0000-000000000001"),
            period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
            close_run_id=UUID("40000000-0000-0000-0000-000000000001"),
            entity_name="Acme",
            currency_code="NGN",
        ),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "ensure_close_run_active_phase",
        lambda **_: (_ for _ in ()).throw(
            JobCancellationRequestedError(
                "Report generation was canceled because the close run is no longer in Reporting."
            )
        ),
    )

    with pytest.raises(JobCancellationRequestedError):
        generate_reports_task._run_report_generation_task(
            close_run_id="40000000-0000-0000-0000-000000000001",
            report_run_id=str(FakeReportRepository.RUN_ID),
            actor_user_id="10000000-0000-0000-0000-000000000001",
            sections=None,
            generate_commentary_flag=False,
            use_llm_commentary=False,
            job_context=FakeCheckpointContext(),
        )

    assert repo_holder["repo"].status_updates[-1]["report_run_id"] == FakeReportRepository.RUN_ID
    assert repo_holder["repo"].status_updates[-1]["status"] == "failed"
    assert "no longer in Reporting" in repo_holder["repo"].status_updates[-1]["failure_reason"]


def test_parse_resume_skips_duplicate_parse_and_store_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure resumed parse jobs rebuild the stored receipt instead of re-parsing the document."""

    parse_record = _build_parse_record()
    monkeypatch.setattr(
        parse_documents_task,
        "get_session_factory",
        lambda: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(
        parse_documents_task,
        "DocumentRepository",
        lambda db_session: FakeDocumentRepository(parse_record=parse_record),
    )
    monkeypatch.setattr(parse_documents_task, "StorageRepository", lambda: SimpleNamespace())
    monkeypatch.setattr(
        parse_documents_task,
        "parse_and_store_document",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("parse_and_store_document should not rerun")
        ),
    )
    monkeypatch.setattr(
        parse_documents_task,
        "_post_process_parsed_document",
        lambda **_: {
            "document_type": "unknown",
            "classification_confidence": None,
            "extraction_created": False,
            "needs_review": True,
            "quality_issue_count": 0,
            "recommendation_job_id": None,
        },
    )

    job_context = FakeCheckpointContext(
        completed_steps={"parse_and_store_document"},
        step_states={
            "parse_and_store_document": parse_documents_task._serialize_parse_pipeline_receipt(
                parse_documents_task.ParsePipelineReceipt(
                    document_version_no=2,
                    parser_name="pdf_parser",
                    parser_version="1.0.0",
                    page_count=3,
                    table_count=1,
                    split_candidate_count=0,
                    checksum="a" * 64,
                    raw_parse_payload={"metadata": {"requires_ocr": False}},
                    derivatives=parse_documents_task.StoredParseDerivatives(
                        normalized_storage_key="normalized/key.pdf",
                        ocr_text_storage_key=None,
                        extracted_tables_storage_key=None,
                    ),
                )
            )
        },
    )

    result = parse_documents_task._run_parse_document_task(
        entity_id=str(parse_record.entity.id),
        close_run_id=str(parse_record.close_run.id),
        document_id=str(parse_record.document.id),
        actor_user_id="10000000-0000-0000-0000-000000000001",
        job_context=job_context,
    )

    assert result["document_version_no"] == 2


def test_report_resume_reuses_checkpointed_artifacts_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure resumed report jobs restore prior artifact refs instead of rebuilding them."""

    repo_holder: dict[str, FakeReportRepository] = {}

    monkeypatch.setattr(
        generate_reports_task,
        "get_session_factory",
        lambda: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "ReportRepository",
        lambda db_session: repo_holder.setdefault("repo", FakeReportRepository()),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_load_report_context",
        lambda **_: SimpleNamespace(
            entity_id=UUID("20000000-0000-0000-0000-000000000001"),
            template_id=UUID("30000000-0000-0000-0000-000000000001"),
            period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
            close_run_id=UUID("40000000-0000-0000-0000-000000000001"),
            entity_name="Acme",
            currency_code="NGN",
        ),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_gather_section_data",
        lambda **_: {"p_and_l": {}},
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_build_excel_report",
        lambda **_: (_ for _ in ()).throw(AssertionError("Excel build should not rerun")),
    )
    monkeypatch.setattr(
        generate_reports_task,
        "_build_pdf_report",
        lambda **_: (_ for _ in ()).throw(AssertionError("PDF build should not rerun")),
    )
    monkeypatch.setattr(generate_reports_task, "ensure_close_run_active_phase", lambda **_: None)

    job_context = FakeCheckpointContext(
        completed_steps={"resolve_report_run", "build_excel_pack", "build_pdf_pack"},
        step_states={
            "resolve_report_run": {
                "report_run_id": str(FakeReportRepository.RUN_ID),
                "version_no": 4,
            },
            "build_excel_pack": {
                "artifact_ref": {
                    "type": "report_excel",
                    "filename": "pack.xlsx",
                    "storage_key": "artifacts/excel",
                    "bucket_kind": "artifacts",
                    "sha256": "b" * 64,
                    "size_bytes": 120,
                }
            },
            "build_pdf_pack": {
                "artifact_ref": {
                    "type": "report_pdf",
                    "filename": "pack.pdf",
                    "storage_key": "artifacts/pdf",
                    "bucket_kind": "artifacts",
                    "sha256": "c" * 64,
                    "size_bytes": 80,
                }
            },
        },
    )

    result = generate_reports_task._run_report_generation_task(
        close_run_id="40000000-0000-0000-0000-000000000001",
        report_run_id=None,
        actor_user_id="10000000-0000-0000-0000-000000000001",
        sections=None,
        generate_commentary_flag=False,
        use_llm_commentary=False,
        job_context=job_context,
    )

    assert len(result["artifact_refs"]) == 2
    assert repo_holder["repo"].status_updates[-1]["status"] == "completed"


def _run_retryable_runner(
    *,
    context: JobRuntimeContext,
    error: Exception,
) -> dict[str, Any]:
    """Persist one checkpoint and then raise the supplied retryable failure."""

    context.checkpoint(step="download_source_document", state={"phase": "download"})
    raise error


def _run_success_runner(
    *,
    context: JobRuntimeContext,
    step: str,
) -> dict[str, Any]:
    """Persist one checkpoint and return a deterministic success payload."""

    context.checkpoint(step=step, state={"phase": step})
    return {"status": "ok"}


class RetryScheduledError(Exception):
    """Signal that the fake tracked task requested a Celery retry."""


class FakeTrackedTask(TrackedJobTask):
    """Run the shared tracked-job wrapper against an in-memory lifecycle controller."""

    def __init__(
        self,
        *,
        controller: InMemoryJobController,
        job_id: UUID,
        retries: int,
        max_retries: int,
    ) -> None:
        """Seed the fake task request metadata expected by the shared wrapper."""

        self.controller = controller
        self.max_retries = max_retries
        self._fake_request = FakeRequest(id=str(job_id), retries=retries)

    @property
    def request(self) -> FakeRequest:
        """Expose a deterministic request object without requiring Celery runtime state."""

        return self._fake_request

    def retry(self, *args: Any, **kwargs: Any) -> None:
        """Capture retry scheduling without requiring Celery runtime state."""

        del args, kwargs
        raise RetryScheduledError()

    def _with_job_service(
        self,
        callback: Callable[[InMemoryJobController], TControllerReturn],
    ) -> TControllerReturn:
        """Route wrapper lifecycle calls into the in-memory controller."""

        return callback(self.controller)

    def run_tracked_job(
        self,
        *,
        runner: Callable[[JobRuntimeContext], dict[str, Any]],
    ) -> dict[str, Any]:
        """Forward explicitly into the shared tracked-job wrapper implementation."""

        return super().run_tracked_job(runner=runner)


class FakeRequest:
    """Expose the minimal request surface consumed by the shared tracked task wrapper."""

    def __init__(self, *, id: str, retries: int) -> None:
        """Capture deterministic task identifier and retry count."""

        self.id = id
        self.retries = retries


class InMemoryJobController:
    """Provide a lightweight durable job lifecycle store for wrapper integration tests."""

    def __init__(self) -> None:
        """Initialize the in-memory job registry."""

        self.jobs: dict[UUID, JobRecord] = {}

    def seed_job(
        self,
        *,
        task_name: str,
        status: JobStatus = JobStatus.QUEUED,
        checkpoint_payload: JsonObject | None = None,
        cancellation_requested_at: datetime | None = None,
    ) -> JobRecord:
        """Create one deterministic job record for tests and return it."""

        job_id = uuid4()
        now = datetime.now(tz=UTC)
        route = resolve_task_route(task_name)
        record = JobRecord(
            id=job_id,
            entity_id=UUID("20000000-0000-0000-0000-000000000001"),
            close_run_id=UUID("30000000-0000-0000-0000-000000000001"),
            document_id=None,
            actor_user_id=UUID("10000000-0000-0000-0000-000000000001"),
            canceled_by_user_id=None,
            resumed_from_job_id=None,
            task_name=task_name,
            queue_name=route.queue.value,
            routing_key=route.routing_key,
            status=status,
            payload={"close_run_id": "close-run-123"},
            checkpoint_payload=dict(checkpoint_payload or {}),
            result_payload=None,
            failure_reason=None,
            failure_details=None,
            blocking_reason=None,
            trace_id=None,
            attempt_count=0,
            retry_count=0,
            max_retries=route.max_retries,
            started_at=None,
            completed_at=None,
            cancellation_requested_at=cancellation_requested_at,
            canceled_at=None,
            dead_lettered_at=None,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job_id] = record
        return record

    def mark_running(self, *, job_id: UUID, trace_id: str | None, attempt_count: int) -> JobRecord:
        """Mirror the job service's running transition for wrapper tests."""

        job = self.jobs[job_id]
        if job.status is JobStatus.CANCELED:
            return job

        updated = replace(
            job,
            status=JobStatus.RUNNING,
            trace_id=trace_id,
            attempt_count=attempt_count,
            started_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self.jobs[job_id] = updated
        return updated

    def record_checkpoint(self, *, job_id: UUID, checkpoint_payload: JsonObject) -> JobRecord:
        """Persist a checkpoint payload for the supplied job."""

        updated = replace(
            self.jobs[job_id],
            checkpoint_payload=dict(checkpoint_payload),
            updated_at=datetime.now(tz=UTC),
        )
        self.jobs[job_id] = updated
        return updated

    def ensure_not_canceled(self, *, job_id: UUID) -> None:
        """Raise the same error the real job service would use for cancellation."""

        from services.jobs.retry_policy import JobCancellationRequestedError

        job = self.jobs[job_id]
        if job.cancellation_requested_at is not None or job.status is JobStatus.CANCELED:
            raise JobCancellationRequestedError(
                "Execution stopped because an operator requested cancellation."
            )

    def mark_retry_scheduled(
        self,
        *,
        job_id: UUID,
        retry_count: int,
        failure_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist retry metadata for the supplied job."""

        updated = replace(
            self.jobs[job_id],
            status=JobStatus.QUEUED,
            retry_count=retry_count,
            failure_reason=failure_reason,
            failure_details=dict(failure_details),
            updated_at=datetime.now(tz=UTC),
        )
        self.jobs[job_id] = updated
        return updated

    def mark_blocked(
        self,
        *,
        job_id: UUID,
        blocking_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist a blocked terminal state for the supplied job."""

        updated = replace(
            self.jobs[job_id],
            status=JobStatus.BLOCKED,
            failure_reason=blocking_reason,
            blocking_reason=blocking_reason,
            failure_details=dict(failure_details),
            updated_at=datetime.now(tz=UTC),
        )
        self.jobs[job_id] = updated
        return updated

    def mark_completed(self, *, job_id: UUID, result_payload: JsonObject) -> JobRecord:
        """Persist a completed terminal state for the supplied job."""

        updated = replace(
            self.jobs[job_id],
            status=JobStatus.COMPLETED,
            result_payload=dict(result_payload),
            completed_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self.jobs[job_id] = updated
        return updated

    def mark_failed(
        self,
        *,
        job_id: UUID,
        failure_reason: str,
        failure_details: JsonObject,
        dead_letter: bool,
    ) -> JobRecord:
        """Persist a failed terminal state for the supplied job."""

        updated = replace(
            self.jobs[job_id],
            status=JobStatus.FAILED,
            failure_reason=failure_reason,
            failure_details=dict(failure_details),
            dead_lettered_at=datetime.now(tz=UTC) if dead_letter else None,
            completed_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self.jobs[job_id] = updated
        return updated

    def mark_canceled(
        self,
        *,
        job_id: UUID,
        failure_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist a canceled terminal state for the supplied job."""

        now = datetime.now(tz=UTC)
        updated = replace(
            self.jobs[job_id],
            status=JobStatus.CANCELED,
            failure_reason=failure_reason,
            failure_details=dict(failure_details),
            cancellation_requested_at=self.jobs[job_id].cancellation_requested_at or now,
            canceled_at=now,
            completed_at=now,
            updated_at=now,
        )
        self.jobs[job_id] = updated
        return updated

    def resume_job(self, *, job_id: UUID, actor_user_id: UUID, reason: str) -> JobRecord:
        """Create a fresh queued job carrying forward the prior checkpoint payload."""

        source_job = self.jobs[job_id]
        resumed_job = self.seed_job(
            task_name=source_job.task_name,
            checkpoint_payload={
                **source_job.checkpoint_payload,
                "resume_reason": reason,
            },
        )
        resumed_job = replace(
            resumed_job,
            actor_user_id=actor_user_id,
            resumed_from_job_id=job_id,
        )
        self.jobs[resumed_job.id] = resumed_job
        return resumed_job


class FakeCheckpointContext:
    """Provide a lightweight checkpoint context for direct task-function tests."""

    def __init__(
        self,
        *,
        completed_steps: set[str] | None = None,
        step_states: dict[str, JsonObject] | None = None,
        cancel_on_call: int | None = None,
    ) -> None:
        """Seed completed steps, persisted step state, and optional cancel timing."""

        self.completed_steps = set(completed_steps or set())
        self.step_states_map = dict(step_states or {})
        self.cancel_on_call = cancel_on_call
        self.ensure_calls = 0

    def checkpoint(self, *, step: str, state: JsonObject | None = None) -> JsonObject:
        """Persist checkpoint state in memory for the direct task tests."""

        self.completed_steps.add(step)
        if state is not None:
            self.step_states_map[step] = dict(state)
        return state or {}

    def ensure_not_canceled(self) -> None:
        """Raise a cancellation error on the configured call count."""

        self.ensure_calls += 1
        if self.cancel_on_call is not None and self.ensure_calls >= self.cancel_on_call:
            raise JobCancellationRequestedError(
                "Execution stopped because an operator requested cancellation."
            )

    def step_completed(self, step: str) -> bool:
        """Return whether the requested step was previously checkpointed."""

        return step in self.completed_steps

    def step_state(self, step: str) -> JsonObject:
        """Return the persisted state for one completed step."""

        return dict(self.step_states_map.get(step, {}))


class FakeSessionContext:
    """Provide a no-op context-managed DB session for direct task-function tests."""

    def __enter__(self) -> SimpleNamespace:
        """Return a placeholder session object."""

        return SimpleNamespace()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Leave exceptions untouched for the direct task tests."""

        del exc_type, exc, tb


class FakeReportRepository:
    """Capture report-run lifecycle mutations for direct report-task tests."""

    RUN_ID = UUID("50000000-0000-0000-0000-000000000001")

    def __init__(self) -> None:
        """Initialize deterministic report-run capture state."""

        self.status_updates: list[dict[str, Any]] = []
        self.created_run = SimpleNamespace(id=self.RUN_ID, version_no=4)

    def get_report_run(self, *, report_run_id: UUID, close_run_id: UUID | None = None) -> Any:
        """Return the seeded run when the expected UUID is requested."""

        del close_run_id
        if report_run_id == self.RUN_ID:
            return self.created_run
        return None

    def next_version_no_for_close_run(self, *, close_run_id: UUID) -> int:
        """Return a deterministic version number for new runs."""

        del close_run_id
        return 4

    def create_report_run(self, **kwargs: Any) -> Any:
        """Return the seeded report run for test execution."""

        del kwargs
        return self.created_run

    def update_report_run_status(self, **kwargs: Any) -> Any:
        """Record one status transition for later assertions."""

        normalized_kwargs = dict(kwargs)
        status_value = normalized_kwargs.get("status")
        if hasattr(status_value, "value"):
            normalized_kwargs["status"] = status_value.value
        self.status_updates.append(normalized_kwargs)
        return self.created_run

    def commit(self) -> None:
        """Provide the report-task commit surface."""

    def rollback(self) -> None:
        """Provide the report-task rollback surface."""


class FakeDocumentRepository:
    """Capture parser status updates while returning one deterministic parse record."""

    def __init__(self, *, parse_record: Any) -> None:
        """Seed the parse record returned to the parser task."""

        self.parse_record = parse_record

    def get_document_for_parse(self, **kwargs: Any) -> Any:
        """Return the seeded parse record."""

        del kwargs
        return self.parse_record

    def update_document_status(self, **kwargs: Any) -> Any:
        """Provide the document-status update surface used by the parser task."""

        del kwargs
        return self.parse_record.document

    def create_activity_event(self, **kwargs: Any) -> None:
        """Provide the parser audit-event surface."""

        del kwargs

    def commit(self) -> None:
        """Provide the parser commit surface."""

    def rollback(self) -> None:
        """Provide the parser rollback surface."""


def _build_parse_record() -> Any:
    """Create a deterministic parse-record stand-in for direct parser-task tests."""

    entity_id = UUID("20000000-0000-0000-0000-000000000001")
    close_run_id = UUID("30000000-0000-0000-0000-000000000001")
    document_id = UUID("40000000-0000-0000-0000-000000000001")
    return SimpleNamespace(
        entity=SimpleNamespace(id=entity_id),
        close_run=SimpleNamespace(
            id=close_run_id,
            entity_id=entity_id,
            period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
        ),
        document=SimpleNamespace(
            id=document_id,
            original_filename="invoice.pdf",
            storage_key="documents/source/invoice.pdf",
        ),
    )


def _build_mutable_job_row(
    *,
    status: JobStatus,
    blocking_reason: str | None = None,
    checkpoint_payload: JsonObject | None = None,
) -> Any:
    """Create a mutable job-row stand-in with the fields `_map_job` expects."""

    now = datetime.now(tz=UTC)
    return SimpleNamespace(
        id=uuid4(),
        entity_id=UUID("20000000-0000-0000-0000-000000000001"),
        close_run_id=UUID("30000000-0000-0000-0000-000000000001"),
        document_id=None,
        actor_user_id=UUID("10000000-0000-0000-0000-000000000001"),
        canceled_by_user_id=None,
        resumed_from_job_id=None,
        task_name=TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK.value,
        queue_name="reporting",
        routing_key="reporting.generate_close_run_pack",
        status=status.value,
        payload={"close_run_id": "close-run-123"},
        checkpoint_payload=dict(checkpoint_payload or {}),
        result_payload=None,
        failure_reason=None,
        failure_details=None,
        blocking_reason=blocking_reason,
        trace_id=None,
        attempt_count=0,
        retry_count=0,
        max_retries=4,
        started_at=None,
        completed_at=None,
        cancellation_requested_at=None,
        canceled_at=None,
        dead_lettered_at=None,
        created_at=now,
        updated_at=now,
    )
TControllerReturn = TypeVar("TControllerReturn")
