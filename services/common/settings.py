"""
Purpose: Centralize environment-backed configuration for the canonical backend runtime.
Scope: Typed settings for API, worker, database, Redis, MinIO,
observability, model gateway, and security concerns.
Dependencies: pydantic-settings, services/common/types.py, and the
.env.example contract at the repository root.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, Field, SecretStr, computed_field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from services.common.types import (
    DeploymentEnvironment,
    PortNumber,
    PositiveInteger,
    Ratio,
    StructuredLogFormat,
)


class RuntimeSettings(BaseModel):
    """Capture cross-service runtime controls that shape local execution behavior."""

    environment: DeploymentEnvironment = Field(default=DeploymentEnvironment.DEVELOPMENT)
    debug: bool = Field(default=False)
    service_name: str = Field(default="accounting-ai-agent", min_length=1)
    timezone_name: str = Field(default="Africa/Lagos", min_length=1)
    api_base_path: str = Field(default="/api", min_length=1)

    @field_validator("api_base_path")
    @classmethod
    def normalize_api_base_path(cls, value: str) -> str:
        """Normalize the API base path into a single leading-slash form."""

        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("API base path cannot be empty.")

        normalized = f"/{stripped_value.lstrip('/')}"
        return normalized.rstrip("/") or "/"


class LoggingSettings(BaseModel):
    """Capture structured logging options shared by API and worker processes."""

    level: str = Field(default="INFO", min_length=1)
    format: StructuredLogFormat = Field(default=StructuredLogFormat.JSON)
    include_stack_info: bool = Field(default=False)
    redact_fields: tuple[str, ...] = Field(
        default=(
            "api_key",
            "authorization",
            "client_secret",
            "cookie",
            "password",
            "secret",
            "session",
            "token",
        )
    )

    @field_validator("level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """Store log level names in their canonical upper-case form."""

        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Log level cannot be empty.")

        return normalized


class ApiSettings(BaseModel):
    """Define loopback API binding settings for the FastAPI server."""

    host: str = Field(default="127.0.0.1", min_length=1)
    port: PortNumber = Field(default=8000)
    reload: bool = Field(default=False)
    request_timeout_seconds: PositiveInteger = Field(default=30)


class WorkerSettings(BaseModel):
    """Define concurrency and lifecycle settings for Celery workers."""

    concurrency: PositiveInteger = Field(default=2)
    prefetch_multiplier: PositiveInteger = Field(default=1)
    max_tasks_per_child: PositiveInteger = Field(default=100)
    task_soft_time_limit_seconds: PositiveInteger = Field(default=1_500)
    task_time_limit_seconds: PositiveInteger = Field(default=1_800)
    result_expires_seconds: PositiveInteger = Field(default=86_400)
    visibility_timeout_seconds: PositiveInteger = Field(default=3_600)

    @model_validator(mode="after")
    def validate_time_limits(self) -> WorkerSettings:
        """Ensure the soft task timeout never exceeds the hard task timeout."""

        if self.task_soft_time_limit_seconds > self.task_time_limit_seconds:
            message = (
                "Worker soft task time limit cannot exceed the hard task time limit. "
                "Update worker_task_soft_time_limit_seconds or "
                "worker_task_time_limit_seconds."
            )
            raise ValueError(message)

        return self


class DatabaseSettings(BaseModel):
    """Define PostgreSQL connectivity and SQLAlchemy behavior for the demo stack."""

    url: str | None = Field(default=None)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: PortNumber = Field(default=5432)
    name: str = Field(default="accounting_agent", min_length=1)
    user: str = Field(default="accounting_agent", min_length=1)
    password: SecretStr = Field(default=SecretStr("accounting_agent"), repr=False)
    schema_name: str = Field(default="public", min_length=1)
    echo_sql: bool = Field(default=False)

    @computed_field(return_type=str)  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        """Build the canonical SQLAlchemy DSN for synchronous database access."""

        if self.url is not None and self.url.strip():
            return _normalize_postgres_url(self.url, for_sqlalchemy=True)

        password = self.password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.user}:{password}@{self.host}:{self.port}/{self.name}"
        )

    @computed_field(return_type=str)  # type: ignore[prop-decorator]
    @property
    def connection_url(self) -> str:
        """Build the canonical libpq-style PostgreSQL DSN for psycopg connectivity checks."""

        if self.url is not None and self.url.strip():
            return _normalize_postgres_url(self.url, for_sqlalchemy=False)

        password = self.password.get_secret_value()
        return f"postgresql://{self.user}:{password}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseModel):
    """Define Redis connection URLs for broker, result backend, and cache usage."""

    url: str | None = Field(default=None)
    broker_url: str = Field(default="redis://127.0.0.1:6379/0", min_length=1)
    result_backend_url: str = Field(default="redis://127.0.0.1:6379/1", min_length=1)
    cache_url: str = Field(default="redis://127.0.0.1:6379/2", min_length=1)

    @model_validator(mode="after")
    def apply_shared_redis_url(self) -> RedisSettings:
        """Derive queue, result, and cache URLs from one provider URL when requested."""

        if self.url is None or not self.url.strip():
            return self

        normalized_url = self.url.strip()
        if self.broker_url == "redis://127.0.0.1:6379/0":
            self.broker_url = _with_redis_database(normalized_url, database_index=0)
        if self.result_backend_url == "redis://127.0.0.1:6379/1":
            self.result_backend_url = _with_redis_database(normalized_url, database_index=1)
        if self.cache_url == "redis://127.0.0.1:6379/2":
            self.cache_url = _with_redis_database(normalized_url, database_index=2)

        return self


class StorageSettings(BaseModel):
    """Define local object-storage connection details and canonical bucket names."""

    url: str | None = Field(default=None)
    bucket_name: str | None = Field(default=None)
    endpoint: str = Field(default="127.0.0.1:9000", min_length=1)
    access_key: str = Field(default="minioadmin", min_length=1)
    secret_key: SecretStr = Field(default=SecretStr("minioadmin"), repr=False)
    secure: bool = Field(default=False)
    region: str = Field(default="us-east-1", min_length=1)
    document_bucket: str = Field(default="close-run-documents", min_length=3)
    artifact_bucket: str = Field(default="close-run-artifacts", min_length=3)
    derivative_bucket: str = Field(default="close-run-derivatives", min_length=3)

    @model_validator(mode="after")
    def apply_hosted_storage_overrides(self) -> StorageSettings:
        """Allow one hosted S3 endpoint and bucket to back all logical storage families."""

        if self.url is not None and self.url.strip():
            parsed_url = urlsplit(self.url.strip())
            if parsed_url.scheme not in {"http", "https"}:
                raise ValueError("Storage URL must use http or https.")
            if not parsed_url.netloc:
                raise ValueError("Storage URL must include a host.")
            self.endpoint = parsed_url.netloc
            self.secure = parsed_url.scheme == "https"

        if self.bucket_name is not None and self.bucket_name.strip():
            shared_bucket_name = self.bucket_name.strip()
            if self.document_bucket == "close-run-documents":
                self.document_bucket = shared_bucket_name
            if self.artifact_bucket == "close-run-artifacts":
                self.artifact_bucket = shared_bucket_name
            if self.derivative_bucket == "close-run-derivatives":
                self.derivative_bucket = shared_bucket_name

        return self

    @computed_field(return_type=str)  # type: ignore[prop-decorator]
    @property
    def endpoint_url(self) -> str:
        """Return the fully qualified object-storage endpoint URL."""

        scheme = "https" if self.secure else "http"
        return f"{scheme}://{self.endpoint}"


class ModelGatewaySettings(BaseModel):
    """Define OpenRouter-backed model gateway connectivity for bounded LLM usage."""

    provider: str = Field(default="openrouter", min_length=1)
    base_url: str = Field(default="https://openrouter.ai/api/v1", min_length=1)
    api_key: SecretStr | None = Field(default=None, repr=False)
    default_model: str = Field(default="openai/gpt-4.1-mini", min_length=1)
    timeout_seconds: PositiveInteger = Field(default=60)


class QuickBooksSettings(BaseModel):
    """Define QuickBooks OAuth and company targeting values for future integrations."""

    client_id: str | None = Field(default=None)
    client_secret: SecretStr | None = Field(default=None, repr=False)
    redirect_uri: str = Field(
        default="http://127.0.0.1:8000/api/integrations/quickbooks/callback",
        min_length=1,
    )
    sandbox_company_id: str | None = Field(default=None)
    use_sandbox: bool = Field(default=True)
    allowed_return_origins: tuple[str, ...] = Field(default=())

    @field_validator("allowed_return_origins", mode="before")
    @classmethod
    def normalize_allowed_return_origins(cls, value: object) -> object:
        """Accept either a JSON array or a comma-delimited env string of allowed browser origins."""

        if isinstance(value, str):
            stripped_value = value.strip()
            if not stripped_value:
                return ()

            if stripped_value.startswith("["):
                try:
                    value = json.loads(stripped_value)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "QuickBooks allowed return origins JSON value must be a valid array."
                    ) from error
            else:
                value = stripped_value.split(",")

        if isinstance(value, (list, tuple)):
            normalized_origins: list[str] = []
            for origin in value:
                if not isinstance(origin, str):
                    raise ValueError(
                        "QuickBooks allowed return origins entries must be strings."
                    )

                normalized_origin = origin.strip().rstrip("/")
                if normalized_origin:
                    normalized_origins.append(normalized_origin)
            return tuple(normalized_origins)
        return value


class ObservabilitySettings(BaseModel):
    """Define trace and metrics export settings for the local observability pipeline."""

    enabled: bool = Field(default=True)
    service_namespace: str = Field(default="accounting-agent", min_length=1)
    otlp_endpoint: str = Field(default="http://127.0.0.1:4317", min_length=1)
    sample_ratio: Ratio = Field(default=1.0)


class SecuritySettings(BaseModel):
    """Define signing secrets that later auth and CLI token flows depend on."""

    session_secret: SecretStr | None = Field(default=None, repr=False)
    token_signing_secret: SecretStr | None = Field(default=None, repr=False)
    credential_encryption_key: SecretStr | None = Field(default=None, repr=False)
    session_cookie_name: str = Field(default="accounting_agent_session", min_length=1)
    session_ttl_hours: PositiveInteger = Field(default=12)
    session_rotation_minutes: PositiveInteger = Field(default=30)


class AppSettings(BaseSettings):
    """Aggregate the canonical backend configuration tree from the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        validate_default=True,
    )

    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    model_gateway: ModelGatewaySettings = Field(default_factory=ModelGatewaySettings)
    quickbooks: QuickBooksSettings = Field(default_factory=QuickBooksSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @computed_field(return_type=str)  # type: ignore[prop-decorator]
    @property
    def api_base_url(self) -> str:
        """Build the base loopback URL that local clients should target."""

        return f"http://{self.api.host}:{self.api.port}{self.runtime.api_base_path}"

    def require_values(
        self,
        *,
        feature_name: str,
        variables: Mapping[str, SecretStr | str | None],
    ) -> None:
        """Fail fast when a later feature is enabled without its required configuration."""

        missing_variables = [
            variable_name
            for variable_name, variable_value in variables.items()
            if _is_missing(variable_value)
        ]
        if missing_variables:
            formatted_variables = ", ".join(sorted(missing_variables))
            message = (
                f"{feature_name} is not configured. Set the following environment variables: "
                f"{formatted_variables}."
            )
            raise ValueError(message)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load settings from flat env names such as `api_host` and `runtime_api_base_path`."""

        return (
            init_settings,
            FlatEnvironmentSettingsSource(settings_cls),
            file_secret_settings,
        )


class FlatEnvironmentSettingsSource(PydanticBaseSettingsSource):
    """Map flat environment variable names into the nested `AppSettings` structure."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        """Capture parsed OS and dotenv environment values for flat-name remapping."""

        super().__init__(settings_cls)
        self._env_source = EnvSettingsSource(settings_cls)
        self._dotenv_source = DotEnvSettingsSource(settings_cls)
        self._field_mapping = _build_flat_field_mapping(settings_cls)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return a null field value because this source materializes all settings at once."""

        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """Build nested settings data from canonical flat env names and fail fast on collisions."""

        raw_values = {
            **self._dotenv_source.env_vars,
            **self._env_source.env_vars,
        }
        resolved: dict[str, dict[str, str | None]] = {}

        for env_name, raw_value in raw_values.items():
            if raw_value is None:
                continue

            mapping = self._field_mapping.get(env_name.casefold())
            if mapping is None:
                continue

            section_name, field_name = mapping
            resolved.setdefault(section_name, {})[field_name] = raw_value

        return resolved


def _build_flat_field_mapping(settings_cls: type[BaseSettings]) -> dict[str, tuple[str, str]]:
    """Build the canonical flat env-name mapping for every nested `AppSettings` field."""

    mapping: dict[str, tuple[str, str]] = {}
    for section_name, section_field in settings_cls.model_fields.items():
        annotation = section_field.annotation
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            continue

        for field_name in annotation.model_fields:
            mapping[f"{section_name}_{field_name}".casefold()] = (section_name, field_name)

    mapping.update(
        {
            "port": ("api", "port"),
            "host": ("api", "host"),
            "database_url": ("database", "url"),
            "postgres_url": ("database", "url"),
            "postgresql_url": ("database", "url"),
            "redis_url": ("redis", "url"),
            "redis_private_url": ("redis", "url"),
            "s3_url": ("storage", "url"),
            "storage_url": ("storage", "url"),
            "endpoint": ("storage", "url"),
            "storage_bucket": ("storage", "bucket_name"),
            "bucket": ("storage", "bucket_name"),
            "bucket_name": ("storage", "bucket_name"),
            "access_key_id": ("storage", "access_key"),
            "secret_access_key": ("storage", "secret_key"),
            "region": ("storage", "region"),
        }
    )

    return mapping


def _normalize_postgres_url(url: str, *, for_sqlalchemy: bool) -> str:
    """Normalize a hosted PostgreSQL URL into the form required by the active client library."""

    stripped_url = url.strip()
    if not stripped_url:
        raise ValueError("Database URL cannot be empty.")

    parsed_url = urlsplit(stripped_url)
    normalized_scheme = parsed_url.scheme
    if normalized_scheme == "postgres":
        normalized_scheme = "postgresql"
    if normalized_scheme == "postgresql+psycopg":
        normalized_scheme = "postgresql"
    if normalized_scheme != "postgresql":
        raise ValueError(
            "Database URL must use postgres://, postgresql://, or postgresql+psycopg://."
        )

    target_scheme = "postgresql+psycopg" if for_sqlalchemy else "postgresql"
    normalized = parsed_url._replace(scheme=target_scheme)
    return urlunsplit(normalized)


def _with_redis_database(url: str, *, database_index: int) -> str:
    """Return one Redis URL rewritten to the requested database index."""

    stripped_url = url.strip()
    if not stripped_url:
        raise ValueError("Redis URL cannot be empty.")

    parsed_url = urlsplit(stripped_url)
    if parsed_url.scheme not in {"redis", "rediss"}:
        raise ValueError("Redis URL must use redis:// or rediss://.")

    normalized_path = f"/{database_index}"
    normalized = SplitResult(
        scheme=parsed_url.scheme,
        netloc=parsed_url.netloc,
        path=normalized_path,
        query=parsed_url.query,
        fragment=parsed_url.fragment,
    )
    return urlunsplit(normalized)


def _is_missing(value: SecretStr | str | None) -> bool:
    """Return True when a settings value is absent after environment parsing."""

    if value is None:
        return True

    if isinstance(value, SecretStr):
        return not value.get_secret_value().strip()

    return not value.strip()


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Load and cache application settings for the current process."""

    return AppSettings()


def reset_settings_cache() -> None:
    """Clear the cached settings instance for tests or controlled reconfiguration."""

    get_settings.cache_clear()
