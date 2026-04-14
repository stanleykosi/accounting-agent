"""
Purpose: Register worker task modules for the canonical Celery application.
Scope: Importable task package namespace used by worker bootstrap and tests.
Dependencies: Individual task modules under apps/worker/app/tasks/.
"""

__all__ = [
    "extract_documents",
    "generate_recommendations",
    "generate_reports",
    "parse_documents",
    "run_reconciliation",
]
