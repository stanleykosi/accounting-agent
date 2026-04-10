"""
Purpose: Mark the FastAPI application package boundary for the canonical backend service.
Scope: Shared import root for API routes, dependencies, and the future
main application entrypoint.
Dependencies: Root Python workspace configuration in pyproject.toml and
shared service modules under services/.
"""

APP_PACKAGE_NAME = "apps.api.app"

__all__ = ["APP_PACKAGE_NAME"]
