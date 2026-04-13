"""
Purpose: Group shared observability helpers for traces, metrics, and context propagation.
Scope: Package marker only. Concrete helpers must be imported from their leaf modules so
stdlib-safe utilities such as redaction do not trigger heavier observability imports.
Dependencies: None at import time by design to avoid circular imports during logging bootstrap.
"""

__all__: list[str] = []
