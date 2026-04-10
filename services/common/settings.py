"""
Purpose: Centralize environment-backed configuration for the canonical backend runtime.
Scope: Typed settings for API, worker, database, Redis, MinIO,
observability, model gateway, and security concerns.
Dependencies: pydantic-settings, services/common/types.py, and the
.env.example contract at the repository root.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
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
                "Update ACCOUNTING_AGENT_WORKER__TASK_SOFT_TIME_LIMIT_SECONDS or "
                "ACCOUNTING_AGENT_WORKER__TASK_TIME_LIMIT_SECONDS."
            )
            raise ValueError(message)

        return self


class DatabaseSettings(BaseModel):
    """Define PostgreSQL connectivity and SQLAlchemy behavior for the demo stack."""

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

        password = self.password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.user}:{password}@{self.host}:{self.port}/{self.name}"
        )


class RedisSettings(BaseModel):
    """Define Redis connection URLs for broker, result backend, and cache usage."""

    broker_url: str = Field(default="redis://127.0.0.1:6379/0", min_length=1)
    result_backend_url: str = Field(default="redis://127.0.0.1:6379/1", min_length=1)
    cache_url: str = Field(default="redis://127.0.0.1:6379/2", min_length=1)


class StorageSettings(BaseModel):
    """Define local object-storage connection details and canonical bucket names."""

    endpoint: str = Field(default="127.0.0.1:9000", min_length=1)
    access_key: str = Field(default="minioadmin", min_length=1)
    secret_key: SecretStr = Field(default=SecretStr("minioadmin"), repr=False)
    secure: bool = Field(default=False)
    region: str = Field(default="us-east-1", min_length=1)
    document_bucket: str = Field(default="close-run-documents", min_length=3)
    artifact_bucket: str = Field(default="close-run-artifacts", min_length=3)
    derivative_bucket: str = Field(default="close-run-derivatives", min_length=3)

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


class AppSettings(BaseSettings):
    """Aggregate the canonical backend configuration tree from the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ACCOUNTING_AGENT_",
        env_nested_delimiter="__",
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
