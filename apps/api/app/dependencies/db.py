"""
Purpose: Expose the canonical FastAPI database-session dependency for API routes.
Scope: Thin dependency aliases that keep route signatures consistent and typed.
Dependencies: FastAPI dependency injection and the shared SQLAlchemy session helper.

Design notes:
- The SQLAlchemy ``Session`` type carries forward-reference annotations to private
  classes (``_SessionBind``) that Pydantic cannot resolve during OpenAPI generation.
- To keep type-checkers happy while preventing the OpenAPI schema generator from
  walking the full SQLAlchemy type tree, we expose ``Session`` only under
  ``TYPE_CHECKING`` and fall back to ``object`` at runtime where FastAPI treats
  the dependency as opaque.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends
from services.db.session import get_db_session

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    # At type-check time, routes see the real SQLAlchemy Session type.
    _SessionType: type[Session] = Session  # noqa: WPS428
else:
    # At runtime, FastAPI sees ``object`` and skips schema generation for the
    # dependency, avoiding unresolved forward-reference errors from SQLAlchemy.
    _SessionType = object

DatabaseSessionDependency = Annotated[_SessionType, Depends(get_db_session)]

__all__ = ["DatabaseSessionDependency"]
