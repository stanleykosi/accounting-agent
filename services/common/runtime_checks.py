"""
Purpose: Provide canonical dependency health checks shared by API and worker service startup.
Scope: PostgreSQL, Redis, and S3-compatible object storage reachability plus required bucket validation.
Dependencies: Shared settings, psycopg, redis-py, and the MinIO client.
"""

from __future__ import annotations

import psycopg
from minio import Minio
from redis import Redis
from services.common.settings import AppSettings


def run_backend_dependency_healthcheck(settings: AppSettings) -> None:
    """Verify that the canonical backend dependencies are reachable for hosted or local startup."""

    verify_database_connectivity(settings)
    verify_redis_connectivity(settings)
    verify_object_storage_connectivity(settings)


def verify_database_connectivity(settings: AppSettings) -> None:
    """Confirm that PostgreSQL is reachable and responds to a trivial query."""

    connect_kwargs: dict[str, object] = {"connect_timeout": 5}
    preferred_hostaddr = settings.database.resolve_preferred_hostaddr()
    if preferred_hostaddr is not None:
        connect_kwargs["hostaddr"] = preferred_hostaddr

    with psycopg.connect(settings.database.connection_url, **connect_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()


def verify_redis_connectivity(settings: AppSettings) -> None:
    """Confirm that the configured Redis broker URL is reachable for task dispatch and cache usage."""

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
    """Confirm that object storage is reachable and that the required physical buckets exist."""

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
            "Object-storage validation failed. Missing required buckets: "
            f"{formatted_bucket_names}."
        )
