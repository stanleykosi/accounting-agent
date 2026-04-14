"""
Purpose: Provide the canonical worker runtime entrypoint for Celery-based background processing.
Scope: Startup parsing, dependency health checks, structured logging, and Celery worker launch.
Dependencies: apps/worker/app/celery_app.py, shared settings/logging, PostgreSQL, Redis, and MinIO.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from services.common.logging import configure_logging, get_logger
from services.common.runtime_checks import run_backend_dependency_healthcheck
from services.common.settings import AppSettings, get_settings
from services.jobs.task_names import task_queue_names


@dataclass(frozen=True)
class RuntimeArguments:
    """Capture the supported command-line controls for the worker runtime."""

    healthcheck: bool


def parse_args(argv: Sequence[str] | None = None) -> RuntimeArguments:
    """Parse runtime arguments for service startup or container health checks."""

    parser = argparse.ArgumentParser(
        prog="python -m apps.worker.app.runtime",
        description="Run the canonical worker runtime for the local demo stack.",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Validate worker dependencies and exit without starting the service loop.",
    )
    parsed_args = parser.parse_args(list(argv) if argv is not None else None)
    return RuntimeArguments(healthcheck=parsed_args.healthcheck)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the worker runtime entrypoint and return a process exit code."""

    arguments = parse_args(argv)
    settings = get_settings()
    configure_logging(settings, service_name="worker")
    logger = get_logger(__name__)

    try:
        if arguments.healthcheck:
            run_dependency_healthcheck(settings)
            logger.info("Worker dependency healthcheck passed.")
            return 0

        run_celery_worker(settings=settings)
    except Exception:
        logger.exception("Worker runtime failed.")
        return 1

    return 0


def run_celery_worker(*, settings: AppSettings) -> None:
    """Validate dependencies and launch the canonical Celery worker process."""

    from apps.worker.app.celery_app import celery_app

    queue_names = task_queue_names(include_dead_letter=False)
    logger = get_logger(__name__)
    run_dependency_healthcheck(settings)
    logger.info(
        "Launching Celery worker runtime.",
        concurrency=settings.worker.concurrency,
        database_host=settings.database.host,
        queue_names=queue_names,
        redis_broker_url=settings.redis.broker_url,
        storage_endpoint=settings.storage.endpoint_url,
    )
    celery_app.worker_main(
        [
            "worker",
            "--loglevel",
            settings.logging.level.lower(),
            "--concurrency",
            str(settings.worker.concurrency),
            "--prefetch-multiplier",
            str(settings.worker.prefetch_multiplier),
            "--max-tasks-per-child",
            str(settings.worker.max_tasks_per_child),
            "--queues",
            ",".join(queue_names),
        ]
    )


def run_dependency_healthcheck(settings: AppSettings) -> None:
    """Verify that the worker can reach its canonical backing services and buckets."""

    run_backend_dependency_healthcheck(settings)


if __name__ == "__main__":
    sys.exit(main())
