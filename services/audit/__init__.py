"""
Purpose: Expose the canonical audit package used by API, worker, and domain services.
Scope: Package marker only; concrete event vocabulary and emitter logic live in sibling modules.
Dependencies: services/audit/events.py and services/audit/service.py, imported directly by callers.
"""
