"""
Purpose: Provide the FastAPI dependency that exposes the shared Celery task dispatcher.
Scope: API dependency wiring only; canonical dispatch behavior lives in services/jobs/dispatcher.py.
Dependencies: FastAPI, shared job dispatcher, and the shared Celery client builder.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from services.jobs.celery import get_api_celery_app
from services.jobs.dispatcher import TaskDispatcher, TaskDispatchReceipt


def get_task_dispatcher() -> TaskDispatcher:
    """Return the canonical task dispatcher dependency for FastAPI routes."""

    return TaskDispatcher(celery_app=get_api_celery_app(), source_surface="api")


TaskDispatcherDependency = Annotated[TaskDispatcher, Depends(get_task_dispatcher)]
__all__ = [
    "TaskDispatchReceipt",
    "TaskDispatcher",
    "TaskDispatcherDependency",
    "get_task_dispatcher",
]
