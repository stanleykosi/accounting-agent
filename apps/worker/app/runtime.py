"""
Purpose: Provide the canonical worker service runtime used by the local demo stack before task routing is layered in.
Scope: Startup validation, dependency connectivity checks, healthcheck execution, structured logging, and a long-running worker process loop.
Dependencies: Shared runtime settings and logging, PostgreSQL, Redis, and MinIO client libraries declared in pyproject.toml.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Event

import psycopg
from minio import Minio
from redis import Redis
from services.common.logging import configure_logging, get_logger
from services.common.settings import AppSettings, get_settings

DEFAULT_HEARTBEAT_SECONDS = 30
SHUTDOWN_EVENT = Event()


@dataclass(frozen=True)
class RuntimeArguments:
    """Capture the supported command-line controls for the worker runtime."""

    heartbeat_seconds: int
    healthcheck: bool


def parse_args(argv: Sequence[str] | None = None) -> RuntimeArguments:
    """Parse runtime arguments for service startup or container health checks."""

    parser = argparse.ArgumentParser(
        prog="python -m apps.worker.app.runtime",
        description="Run the canonical worker runtime for the local demo stack.",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=_positive_integer,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help="Number of seconds between worker heartbeat log events.",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Validate worker dependencies and exit without starting the service loop.",
    )
    parsed_args = parser.parse_args(list(argv) if argv is not None else None)
    return RuntimeArguments(
        heartbeat_seconds=parsed_args.heartbeat_seconds,
        healthcheck=parsed_args.healthcheck,
    )


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

        run_service_loop(settings=settings, heartbeat_seconds=arguments.heartbeat_seconds)
        return 0
    except Exception:
        logger.exception("Worker runtime failed.")
        return 1


def run_service_loop(*, settings: AppSettings, heartbeat_seconds: int) -> None:
    """Start the long-running worker process loop after fail-fast dependency validation."""

    logger = get_logger(__name__)
    run_dependency_healthcheck(settings)
    install_signal_handlers()
    logger.info(
        "Worker runtime started.",
        heartbeat_seconds=heartbeat_seconds,
        database_host=settings.database.host,
        redis_broker_url=settings.redis.broker_url,
        storage_endpoint=settings.storage.endpoint_url,
    )

    while not SHUTDOWN_EVENT.wait(timeout=heartbeat_seconds):
        logger.info("Worker runtime heartbeat.", heartbeat_seconds=heartbeat_seconds)

    logger.info("Worker runtime stopping after shutdown signal.")


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


def install_signal_handlers() -> None:
    """Install POSIX signal handlers so container shutdown stays graceful and explicit."""

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)


def _positive_integer(value: str) -> int:
    """Parse a strictly positive integer for heartbeat configuration."""

    try:
        parsed_value = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected an integer value.") from error

    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("Expected a value greater than zero.")

    return parsed_value


def _request_shutdown(_: int, __: object | None) -> None:
    """Record a shutdown request from the container runtime without raising immediately."""

    SHUTDOWN_EVENT.set()


if __name__ == "__main__":
    sys.exit(main())
