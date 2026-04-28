"""
Purpose: Mark the Celery worker application package boundary for the canonical async service.
Scope: Shared import root for worker bootstrap, task modules, and worker-side
audit hooks.
Dependencies: Root Python workspace configuration in pyproject.toml and
shared service modules under services/.
"""

WORKER_PACKAGE_NAME = "apps.worker.app"

__all__ = ["WORKER_PACKAGE_NAME"]
