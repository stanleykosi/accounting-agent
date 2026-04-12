"""
Purpose: Define the chart-of-accounts service package boundary.
Scope: Re-export canonical COA service primitives used by API routes and tests.
Dependencies: services/coa/service.py and companion COA modules.
"""

from services.coa.service import (
    CoaService,
    CoaServiceError,
    CoaServiceErrorCode,
)

__all__ = ["CoaService", "CoaServiceError", "CoaServiceErrorCode"]
