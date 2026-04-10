"""
Purpose: Define shared low-level enums and typed primitives for backend
infrastructure modules.
Scope: Deployment environment values, structured log output modes, JSON
type aliases, and time helpers.
Dependencies: Used by services/common/settings.py and
services/common/logging.py before domain enums exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
type JsonObject = dict[str, JsonValue]
PortNumber = Annotated[int, Field(ge=1, le=65535)]
PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]


class DeploymentEnvironment(StrEnum):
    """Enumerate the supported runtime environments for local and packaged deployments."""

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class StructuredLogFormat(StrEnum):
    """Enumerate the supported log renderers for machine and operator consumption."""

    JSON = "json"
    CONSOLE = "console"


def utc_now() -> datetime:
    """Return the current aware timestamp in UTC for traceable event creation."""

    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalize a timestamp into UTC and fail fast for naive datetimes."""

    if value.tzinfo is None:
        message = "Naive datetimes are not allowed. Pass an aware datetime with timezone data."
        raise ValueError(message)

    return value.astimezone(UTC)
