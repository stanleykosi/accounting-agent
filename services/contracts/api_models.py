"""
Purpose: Define seed Pydantic API contract models that anchor the OpenAPI schema.
Scope: Health, metadata, and route-descriptor models used by the FastAPI entrypoint
and SDK generation.
Dependencies: Pydantic and shared backend primitive types from services/common.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from services.common.types import DeploymentEnvironment


class ContractModel(BaseModel):
    """Provide strict contract defaults for API-facing Pydantic models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiHealthStatus(ContractModel):
    """Describe the current API process health for operators and generated SDK consumers."""

    status: Literal["ok"] = Field(
        description="Deterministic health status used by local monitors and contract checks."
    )
    service_name: str = Field(
        min_length=1,
        description="Logical service name for this API process.",
    )
    environment: DeploymentEnvironment = Field(
        description="Active runtime environment for the current backend process."
    )
    version: str = Field(
        min_length=1,
        description="Semantic application version exposed by the API.",
    )
    api_base_path: str = Field(
        min_length=1,
        description="Base path prefix that all API routes are mounted beneath.",
    )
    generated_at: datetime = Field(
        description="UTC timestamp indicating when the response payload was generated."
    )


class ApiRouteDescriptor(ContractModel):
    """Describe one public API route exposed by the current FastAPI application."""

    name: str = Field(
        min_length=1,
        description="Stable route name and OpenAPI operation ID seed.",
    )
    path: str = Field(
        min_length=1,
        description="Fully qualified path pattern exposed by the API.",
    )
    methods: tuple[str, ...] = Field(
        min_length=1,
        description="HTTP methods supported by the route in deterministic sorted order.",
    )
    summary: str | None = Field(
        default=None,
        description="Human-readable route summary exposed in OpenAPI and docs.",
    )
    tags: tuple[str, ...] = Field(
        default=(),
        description=(
            "OpenAPI tags assigned to the route for grouping and generated client discovery."
        ),
    )


class ApiContractMetadata(ContractModel):
    """Describe contract-level API metadata needed by local tooling and generated clients."""

    service_name: str = Field(
        min_length=1,
        description="Logical service name for the API process.",
    )
    version: str = Field(
        min_length=1,
        description="Semantic version of the running API contract.",
    )
    api_base_path: str = Field(
        min_length=1,
        description="Base path prefix shared by the public API routes.",
    )
    openapi_url: str = Field(
        min_length=1,
        description="Route where the canonical OpenAPI schema can be downloaded.",
    )
    docs_url: str = Field(
        min_length=1,
        description="Interactive API documentation URL exposed by FastAPI.",
    )
    routes: tuple[ApiRouteDescriptor, ...] = Field(
        default=(),
        description=(
            "Deterministic catalog of public API routes that belong to the contract surface."
        ),
    )
