"""
Purpose: Collect entity-domain services for workspace lifecycle, memberships, and timeline reads.
Scope: Package marker for the canonical entity service modules.
Dependencies: Individual service modules under services/entity/.
"""

from services.entity.service import EntityService, EntityServiceError, EntityServiceErrorCode

__all__ = ["EntityService", "EntityServiceError", "EntityServiceErrorCode"]
