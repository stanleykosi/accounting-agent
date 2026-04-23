"""
Purpose: Provide canonical dependency health checks shared by API and worker service startup.
Scope: PostgreSQL, Redis, and S3-compatible object storage reachability plus
required bucket validation.
Dependencies: Shared settings, psycopg, redis-py, and the MinIO client.
"""

from __future__ import annotations

import shutil
import subprocess

import psycopg
from minio import Minio
from redis import Redis
from redis.exceptions import (
    AuthenticationError as RedisAuthenticationError,
)
from redis.exceptions import (
    BusyLoadingError as RedisBusyLoadingError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)
from services.common.settings import AppSettings
from urllib3 import PoolManager, Timeout
from urllib3.exceptions import HTTPError as UrllibHttpError

_TRANSIENT_NETWORK_ERROR_TOKENS = (
    "connection refused",
    "connection reset by peer",
    "network is unreachable",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


class TransientDependencyCheckError(RuntimeError):
    """Signal that a dependency probe failed for a retryable warmup reason."""


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

    try:
        with psycopg.connect(settings.database.connection_url, **connect_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
    except psycopg.OperationalError as error:
        if _is_transient_database_operational_error(error):
            _raise_transient_dependency_check_error(
                dependency_name="Database",
                error=error,
            )
        raise
    except (OSError, TimeoutError) as error:
        if _looks_like_transient_network_error(error):
            _raise_transient_dependency_check_error(
                dependency_name="Database",
                error=error,
            )
        raise


def verify_redis_connectivity(settings: AppSettings) -> None:
    """Confirm that the configured Redis broker URL is reachable for tasks and cache usage."""

    client = Redis.from_url(
        settings.redis.broker_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        try:
            client.ping()
        except RedisAuthenticationError:
            raise
        except RedisBusyLoadingError as error:
            _raise_transient_dependency_check_error(
                dependency_name="Redis",
                error=error,
            )
        except (RedisConnectionError, RedisTimeoutError, OSError) as error:
            if _looks_like_transient_network_error(error):
                _raise_transient_dependency_check_error(
                    dependency_name="Redis",
                    error=error,
                )
            raise
    finally:
        client.close()


def verify_object_storage_connectivity(settings: AppSettings) -> None:
    """Confirm that object storage is reachable and that the required physical buckets exist."""

    http_client = PoolManager(
        retries=False,
        timeout=Timeout(connect=5.0, read=5.0),
    )
    client = Minio(
        endpoint=settings.storage.endpoint,
        access_key=settings.storage.access_key,
        secret_key=settings.storage.secret_key.get_secret_value(),
        secure=settings.storage.secure,
        region=settings.storage.region,
        http_client=http_client,
    )
    try:
        try:
            bucket_names = {bucket.name for bucket in client.list_buckets()}
        except (UrllibHttpError, OSError, TimeoutError) as error:
            if _looks_like_transient_network_error(error):
                _raise_transient_dependency_check_error(
                    dependency_name="Object storage",
                    error=error,
                )
            raise
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
    finally:
        http_client.clear()


def verify_ocr_runtime() -> None:
    """Confirm that the worker host has the OCR binaries and language packs we rely on."""

    for binary_name in ("ocrmypdf", "tesseract"):
        if shutil.which(binary_name) is None:
            raise RuntimeError(
                "OCR runtime validation failed. Missing required binary: "
                f"{binary_name}. Install OCRmyPDF and Tesseract in the worker image."
            )

    try:
        completed = subprocess.run(
            ["tesseract", "--list-langs"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "OCR runtime validation failed. Tesseract language discovery timed out."
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or error.stdout or "no process output").strip()
        raise RuntimeError(
            "OCR runtime validation failed. Could not enumerate Tesseract language packs: "
            f"{stderr}"
        ) from error

    available_languages = {
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip() and "available languages" not in line.lower()
    }
    required_languages = {"eng", "osd"}
    missing_languages = sorted(required_languages - available_languages)
    if missing_languages:
        formatted_languages = ", ".join(missing_languages)
        raise RuntimeError(
            "OCR runtime validation failed. Missing required Tesseract language packs: "
            f"{formatted_languages}."
        )


def _raise_transient_dependency_check_error(
    *,
    dependency_name: str,
    error: Exception,
) -> None:
    """Raise one stable retryable dependency error with the original exception chained."""

    raise TransientDependencyCheckError(
        f"{dependency_name} dependency is not reachable yet: "
        f"{_normalize_dependency_error_message(error)}"
    ) from error


def _is_transient_database_operational_error(error: psycopg.OperationalError) -> bool:
    """Return whether one PostgreSQL startup failure is safe to retry."""

    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate.startswith("08"):
        return True
    return _looks_like_transient_network_error(error)


def _looks_like_transient_network_error(error: BaseException) -> bool:
    """Return whether one dependency error looks like a retryable transport failure."""

    for candidate in _iter_exception_chain(error):
        message = _normalize_dependency_error_message(candidate).lower()
        if any(token in message for token in _TRANSIENT_NETWORK_ERROR_TOKENS):
            return True
    return False


def _iter_exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Return the causal chain for one exception without looping forever."""

    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, BaseException) else None
    return tuple(chain)


def _normalize_dependency_error_message(error: BaseException) -> str:
    """Return one compact dependency error string for readiness payloads and logs."""

    message = str(error).strip()
    if message:
        return message
    return error.__class__.__name__
