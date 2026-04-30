"""
Purpose: Bootstrap the canonical Celery worker application and register the worker task set.
Scope: Imports task modules for registration while exposing the shared Celery runtime symbols.
Dependencies: Celery runtime configuration and worker task modules.
"""

from __future__ import annotations

import apps.worker.app.tasks.extract_documents as _extract_documents  # noqa: F401
import apps.worker.app.tasks.execute_chat_operator as _execute_chat_operator  # noqa: F401
import apps.worker.app.tasks.generate_exports as _generate_exports  # noqa: F401
import apps.worker.app.tasks.generate_recommendations as _generate_recommendations  # noqa: F401
import apps.worker.app.tasks.generate_reports as _generate_reports  # noqa: F401
import apps.worker.app.tasks.parse_documents as _parse_documents  # noqa: F401
import apps.worker.app.tasks.resume_chat_operator as _resume_chat_operator  # noqa: F401
import apps.worker.app.tasks.run_reconciliation as _run_reconciliation  # noqa: F401
from apps.worker.app.celery_runtime import ObservedTask, celery_app, run_trace_probe

__all__ = ["ObservedTask", "celery_app", "run_trace_probe"]
