"""
Purpose: Expose the canonical database package boundary for ORM metadata and models.
Scope: Shared imports used by Alembic, repositories, and service-layer database access.
Dependencies: services/db/base.py and services/db/models/.
"""

from services.db.base import Base

__all__ = ["Base"]
