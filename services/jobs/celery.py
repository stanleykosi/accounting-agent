"""
Purpose: Build the canonical Celery application configuration shared by API and worker services.
Scope: Broker/back-end wiring, queue declarations, routing rules,
and cached API-side Celery clients.
Dependencies: Celery, Kombu, services/common/settings.py, and services/jobs/task_names.py.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from celery import Celery
from kombu import Exchange, Queue
from services.common.settings import AppSettings, get_settings
from services.jobs.task_names import TaskQueue, task_queue_names, task_routes_for_celery

DEFAULT_CELERY_APP_NAME = "accounting-ai-agent"
TASK_EXCHANGE_NAME = "accounting-agent.tasks"


def build_task_exchange() -> Exchange:
    """Create the durable topic exchange used by all canonical background jobs."""

    return Exchange(name=TASK_EXCHANGE_NAME, type="topic", durable=True)


def build_task_queues(*, include_dead_letter: bool = True) -> tuple[Queue, ...]:
    """Create the canonical queue declarations used by worker startup and task routing."""

    exchange = build_task_exchange()
    queues: list[Queue] = []

    for queue_name in task_queue_names(include_dead_letter=include_dead_letter):
        routing_key = f"{queue_name}.#"
        queues.append(
            Queue(
                name=queue_name,
                exchange=exchange,
                routing_key=routing_key,
                durable=True,
            )
        )

    return tuple(queues)


def build_celery_configuration(settings: AppSettings) -> dict[str, Any]:
    """Render the shared Celery configuration from validated application settings."""

    return {
        "accept_content": ["json"],
        "broker_connection_retry_on_startup": True,
        "broker_transport_options": {
            "visibility_timeout": settings.worker.visibility_timeout_seconds,
        },
        "enable_utc": True,
        "result_backend": settings.redis.result_backend_url,
        "result_expires": settings.worker.result_expires_seconds,
        "result_serializer": "json",
        "task_acks_late": True,
        "task_create_missing_queues": False,
        "task_default_exchange": TASK_EXCHANGE_NAME,
        "task_default_exchange_type": "topic",
        "task_default_queue": TaskQueue.CONTROL.value,
        "task_default_routing_key": f"{TaskQueue.CONTROL.value}.default",
        "task_ignore_result": False,
        "task_queues": build_task_queues(),
        "task_routes": task_routes_for_celery(),
        "task_serializer": "json",
        "task_send_sent_event": True,
        "task_soft_time_limit": settings.worker.task_soft_time_limit_seconds,
        "task_time_limit": settings.worker.task_time_limit_seconds,
        "task_track_started": True,
        "timezone": settings.runtime.timezone_name,
        "worker_hijack_root_logger": False,
        "worker_max_tasks_per_child": settings.worker.max_tasks_per_child,
        "worker_prefetch_multiplier": settings.worker.prefetch_multiplier,
        "worker_send_task_events": True,
    }


def create_celery_app(*, settings: AppSettings | None = None) -> Celery:
    """Create a Celery app configured for the canonical local async runtime."""

    resolved_settings = settings or get_settings()
    celery_app = Celery(
        DEFAULT_CELERY_APP_NAME,
        broker=resolved_settings.redis.broker_url,
        backend=resolved_settings.redis.result_backend_url,
    )
    celery_app.conf.update(build_celery_configuration(resolved_settings))
    return celery_app


@lru_cache(maxsize=1)
def get_api_celery_app() -> Celery:
    """Return the cached Celery client used by the API process for task dispatch."""

    return create_celery_app()


__all__ = [
    "DEFAULT_CELERY_APP_NAME",
    "TASK_EXCHANGE_NAME",
    "build_celery_configuration",
    "build_task_exchange",
    "build_task_queues",
    "create_celery_app",
    "get_api_celery_app",
]
