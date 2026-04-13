"""
Purpose: Build entity/close-run/period grounding context for chat threads.
Scope: Resolve the accounting context snapshot (entity name, close run period,
autonomy mode, base currency) that grounds every assistant response so that
chat stays strictly scoped to the current workflow state.
Dependencies: Entity and close-run repository records, chat contracts,
and the canonical enum definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from services.auth.service import serialize_uuid
from services.contracts.chat_models import GroundingContext
from services.db.repositories.close_run_repo import (
    CloseRunRecord,
    CloseRunRepository,
)
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityRecord,
    EntityRepository,
)


class ChatGroundingErrorCode(StrEnum):
    """Enumerate the stable error codes surfaced by chat grounding."""

    ENTITY_NOT_FOUND = "entity_not_found"
    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    ACCESS_DENIED = "access_denied"
    INVALID_SCOPE = "invalid_scope"


class ChatGroundingError(Exception):
    """Represent an expected grounding failure that API routes should expose cleanly."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ChatGroundingErrorCode,
        message: str,
    ) -> None:
        """Capture the HTTP status, stable error code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class GroundingContextRecord:
    """Describe the resolved grounding context enriched with ORM records."""

    entity: EntityRecord
    close_run: CloseRunRecord | None
    context: GroundingContext


class ChatGroundingService:
    """Resolve and validate the entity/close-run/period context for chat threads."""

    def __init__(
        self,
        *,
        entity_repo: EntityRepository,
        close_run_repo: CloseRunRepository,
    ) -> None:
        """Capture the persistence boundaries used by grounding workflows."""

        self._entity_repo = entity_repo
        self._close_run_repo = close_run_repo

    def resolve_context(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        user_id: UUID,
    ) -> GroundingContextRecord:
        """Resolve the grounding context or raise a canonical grounding error.

        This method verifies that the caller has access to the entity, and if a
        close run is specified, that it belongs to the entity and is accessible.
        The returned GroundingContext contains the serializable snapshot stored
        in the chat thread's context_payload column.
        """

        entity_access = self._load_entity_access(entity_id=entity_id, user_id=user_id)

        close_run: CloseRunRecord | None = None
        if close_run_id is not None:
            close_run = self._load_close_run_access(
                close_run_id=close_run_id,
                entity_id=entity_id,
                user_id=user_id,
            )

        context = self._build_grounding_context(
            entity=entity_access.entity,
            close_run=close_run,
        )
        return GroundingContextRecord(
            entity=entity_access.entity,
            close_run=close_run,
            context=context,
        )

    def build_context_payload(self, *, context: GroundingContext) -> dict[str, Any]:
        """Serialize a grounding context into the JSONB payload stored on threads."""

        return {
            "entity_id": context.entity_id,
            "entity_name": context.entity_name,
            "close_run_id": context.close_run_id,
            "period_label": context.period_label,
            "autonomy_mode": context.autonomy_mode,
            "base_currency": context.base_currency,
        }

    def parse_context_payload(self, *, payload: dict[str, Any]) -> GroundingContext:
        """Deserialize a stored context payload back into a grounding context contract."""

        return GroundingContext(
            entity_id=payload["entity_id"],
            entity_name=payload["entity_name"],
            close_run_id=payload.get("close_run_id"),
            period_label=payload.get("period_label"),
            autonomy_mode=payload["autonomy_mode"],
            base_currency=payload["base_currency"],
        )

    def _load_entity_access(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityAccessRecord:
        """Load an accessible entity workspace or raise the canonical access error."""

        access = self._entity_repo.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if access is None:
            raise ChatGroundingError(
                status_code=404,
                code=ChatGroundingErrorCode.ACCESS_DENIED,
                message="That workspace does not exist or is not accessible to the current user.",
            )

        return access

    def _load_close_run_access(
        self,
        *,
        close_run_id: UUID,
        entity_id: UUID,
        user_id: UUID,
    ) -> CloseRunRecord:
        """Load an accessible close run or raise the canonical access error."""

        access = self._close_run_repo.get_close_run_for_user(
            close_run_id=close_run_id,
            entity_id=entity_id,
            user_id=user_id,
        )
        if access is None:
            raise ChatGroundingError(
                status_code=404,
                code=ChatGroundingErrorCode.CLOSE_RUN_NOT_FOUND,
                message="That close run does not exist or is not accessible in this workspace.",
            )

        return access.close_run

    def _build_grounding_context(
        self,
        *,
        entity: EntityRecord,
        close_run: CloseRunRecord | None,
    ) -> GroundingContext:
        """Build the serializable grounding context from resolved records."""

        period_label: str | None = None
        if close_run is not None:
            start = close_run.period_start.strftime("%b %Y")
            end = close_run.period_end.strftime("%b %Y")
            if start == end:
                period_label = start
            else:
                period_label = f"{start} - {end}"

        return GroundingContext(
            entity_id=serialize_uuid(entity.id),
            entity_name=entity.name,
            close_run_id=serialize_uuid(close_run.id) if close_run is not None else None,
            period_label=period_label,
            autonomy_mode=entity.autonomy_mode.value,
            base_currency=entity.base_currency,
        )


__all__ = [
    "ChatGroundingError",
    "ChatGroundingErrorCode",
    "ChatGroundingService",
    "GroundingContextRecord",
]
