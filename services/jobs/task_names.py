"""
Purpose: Define the canonical Celery task names, queues, and routing keys for the platform.
Scope: Shared async-job vocabulary used by API dispatch helpers,
worker routing, and operator documentation.
Dependencies: Standard-library enums/dataclasses and Celery configuration modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskQueue(StrEnum):
    """Enumerate the queue lanes used by the canonical background-job topology."""

    CONTROL = "control"
    DOCUMENTS = "documents"
    ACCOUNTING = "accounting"
    REPORTING = "reporting"
    INTEGRATIONS = "integrations"
    DEAD_LETTER = "dead_letter"


class TaskName(StrEnum):
    """Enumerate canonical Celery task identifiers that must not drift across services."""

    SYSTEM_TRACE_PROBE = "system.trace_probe"
    DOCUMENT_PARSE_AND_EXTRACT = "documents.parse_and_extract"
    DOCUMENT_REPROCESS = "documents.reprocess_document"
    ACCOUNTING_RECOMMEND_CLOSE_RUN = "accounting.recommend_close_run"
    RECONCILIATION_EXECUTE_CLOSE_RUN = "reconciliation.execute_close_run"
    REPORTING_GENERATE_CLOSE_RUN_PACK = "reporting.generate_close_run_pack"
    QUICKBOOKS_SYNC_CHART_OF_ACCOUNTS = "integrations.quickbooks.sync_chart_of_accounts"


@dataclass(frozen=True, slots=True)
class TaskRouteDefinition:
    """Describe the stable queue lane, routing key, and retry budget for one task family."""

    queue: TaskQueue
    routing_key: str
    max_retries: int


TASK_ROUTE_DEFINITIONS: dict[TaskName, TaskRouteDefinition] = {
    TaskName.SYSTEM_TRACE_PROBE: TaskRouteDefinition(
        queue=TaskQueue.CONTROL,
        routing_key="control.system.trace_probe",
        max_retries=3,
    ),
    TaskName.DOCUMENT_PARSE_AND_EXTRACT: TaskRouteDefinition(
        queue=TaskQueue.DOCUMENTS,
        routing_key="documents.parse_and_extract",
        max_retries=5,
    ),
    TaskName.DOCUMENT_REPROCESS: TaskRouteDefinition(
        queue=TaskQueue.DOCUMENTS,
        routing_key="documents.reprocess_document",
        max_retries=5,
    ),
    TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN: TaskRouteDefinition(
        queue=TaskQueue.ACCOUNTING,
        routing_key="accounting.recommend_close_run",
        max_retries=4,
    ),
    TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN: TaskRouteDefinition(
        queue=TaskQueue.ACCOUNTING,
        routing_key="accounting.reconciliation.execute_close_run",
        max_retries=4,
    ),
    TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK: TaskRouteDefinition(
        queue=TaskQueue.REPORTING,
        routing_key="reporting.generate_close_run_pack",
        max_retries=4,
    ),
    TaskName.QUICKBOOKS_SYNC_CHART_OF_ACCOUNTS: TaskRouteDefinition(
        queue=TaskQueue.INTEGRATIONS,
        routing_key="integrations.quickbooks.sync_chart_of_accounts",
        max_retries=4,
    ),
}


def resolve_task_name(task_name: TaskName | str) -> TaskName:
    """Resolve a task identifier into the canonical enum or fail fast on unknown names."""

    if isinstance(task_name, TaskName):
        return task_name

    try:
        return TaskName(task_name)
    except ValueError as error:
        message = f"Unknown task name '{task_name}'. Register it in services/jobs/task_names.py."
        raise ValueError(message) from error


def resolve_task_route(task_name: TaskName | str) -> TaskRouteDefinition:
    """Return the queue and routing metadata for a canonical task."""

    resolved_task_name = resolve_task_name(task_name)
    return TASK_ROUTE_DEFINITIONS[resolved_task_name]


def task_queue_names(*, include_dead_letter: bool = True) -> tuple[str, ...]:
    """Return deterministic queue names for worker startup and diagnostics."""

    queues = [queue.value for queue in TaskQueue]
    if not include_dead_letter:
        queues.remove(TaskQueue.DEAD_LETTER.value)

    return tuple(queues)


def task_routes_for_celery() -> dict[str, dict[str, str]]:
    """Render the canonical route table in the structure expected by Celery config."""

    return {
        task_name.value: {
            "queue": definition.queue.value,
            "routing_key": definition.routing_key,
        }
        for task_name, definition in TASK_ROUTE_DEFINITIONS.items()
    }


__all__ = [
    "TASK_ROUTE_DEFINITIONS",
    "TaskName",
    "TaskQueue",
    "TaskRouteDefinition",
    "resolve_task_name",
    "resolve_task_route",
    "task_queue_names",
    "task_routes_for_celery",
]
