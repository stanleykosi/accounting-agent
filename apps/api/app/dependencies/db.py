"""
Purpose: Expose the canonical FastAPI database-session dependency for API routes.
Scope: Thin dependency aliases that keep route signatures consistent and typed.
Dependencies: FastAPI dependency injection and the shared SQLAlchemy session helper.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from services.db.session import get_db_session
from sqlalchemy.orm import Session

DatabaseSessionDependency = Annotated[Session, Depends(get_db_session)]

__all__ = ["DatabaseSessionDependency"]
