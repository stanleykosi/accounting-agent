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

import psycopg
from minio import Minio
from redis import Redis
from services.common.logging import configure_logging, get_logger
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

    verify_database_connectivity(settings)
    verify_redis_connectivity(settings)
    verify_object_storage_connectivity(settings)


def verify_database_connectivity(settings: AppSettings) -> None:
    """Confirm that PostgreSQL is reachable and responds to a trivial query."""

    with psycopg.connect(
        dbname=settings.database.name,
        host=settings.database.host,
        password=settings.database.password.get_secret_value(),
        port=settings.database.port,
        user=settings.database.user,
        connect_timeout=5,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()


def verify_redis_connectivity(settings: AppSettings) -> None:
    """Confirm that the configured Redis broker URL is reachable for future task dispatch."""

    client = Redis.from_url(
        settings.redis.broker_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        client.ping()
    finally:
        client.close()


def verify_object_storage_connectivity(settings: AppSettings) -> None:
    """Confirm that MinIO is reachable and that the canonical buckets already exist."""

    client = Minio(
        endpoint=settings.storage.endpoint,
        access_key=settings.storage.access_key,
        secret_key=settings.storage.secret_key.get_secret_value(),
        secure=settings.storage.secure,
        region=settings.storage.region,
    )
    bucket_names = {bucket.name for bucket in client.list_buckets()}
    required_bucket_names = {
        settings.storage.document_bucket,
        settings.storage.artifact_bucket,
        settings.storage.derivative_bucket,
    }
    missing_bucket_names = sorted(required_bucket_names - bucket_names)
    if missing_bucket_names:
        formatted_bucket_names = ", ".join(missing_bucket_names)
        raise RuntimeError(
            "Worker object-storage validation failed. Missing required MinIO buckets: "
            f"{formatted_bucket_names}."
        )


if __name__ == "__main__":
    sys.exit(main())
