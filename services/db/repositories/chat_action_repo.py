"""
Purpose: Persist and query chat action plans (proposed changes, approval
requests, and workflow actions) through SQLAlchemy.
Scope: CRUD operations for chat-originated action execution plans, status
transitions, and review-queue queries for the chat action routing service.
Dependencies: SQLAlchemy ORM sessions plus the canonical chat action plan
model under services/db/models/chat_action_plans.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from services.db.models.chat_action_plans import ChatActionPlan
from sqlalchemy import desc, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ChatActionPlanRecord:
    """Describe one chat action plan row consumed by the action routing service."""

    id: UUID
    thread_id: UUID
    message_id: UUID | None
    entity_id: UUID
    close_run_id: UUID | None
    actor_user_id: UUID
    intent: str
    target_type: str | None
    target_id: UUID | None
    payload: dict[str, Any]
    confidence: float
    autonomy_mode: str
    status: str
    requires_human_approval: bool
    reasoning: str
    applied_result: dict[str, Any] | None
    rejected_reason: str | None
    superseded_by_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ChatActionRepository:
    """Execute canonical chat action plan persistence operations within one SQLAlchemy session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by the action router."""
        self._db_session = db_session

    def create_action_plan(
        self,
        *,
        thread_id: UUID,
        message_id: UUID | None,
        entity_id: UUID,
        close_run_id: UUID | None,
        actor_user_id: UUID,
        intent: str,
        target_type: str | None,
        target_id: UUID | None,
        payload: dict[str, Any],
        confidence: float,
        autonomy_mode: str,
        requires_human_approval: bool,
        reasoning: str,
    ) -> ChatActionPlanRecord:
        """Stage a new chat action plan and flush it so it can be referenced."""

        plan = ChatActionPlan(
            thread_id=thread_id,
            message_id=message_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor_user_id=actor_user_id,
            intent=intent,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            confidence=confidence,
            autonomy_mode=autonomy_mode,
            requires_human_approval=requires_human_approval,
            reasoning=reasoning,
        )
        self._db_session.add(plan)
        self._db_session.flush()
        return _map_action_plan(plan)

    def get_action_plan_by_id(
        self,
        *,
        action_plan_id: UUID,
    ) -> ChatActionPlanRecord | None:
        """Return one action plan by UUID or None when it does not exist."""

        statement = select(ChatActionPlan).where(ChatActionPlan.id == action_plan_id)
        plan = self._db_session.execute(statement).scalar_one_or_none()
        if plan is None:
            return None
        return _map_action_plan(plan)

    def get_action_plan_for_thread(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
    ) -> ChatActionPlanRecord | None:
        """Return one action plan when it belongs to the specified thread."""

        statement = select(ChatActionPlan).where(
            ChatActionPlan.id == action_plan_id,
            ChatActionPlan.thread_id == thread_id,
        )
        plan = self._db_session.execute(statement).scalar_one_or_none()
        if plan is None:
            return None
        return _map_action_plan(plan)

    def list_pending_actions_for_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        limit: int = 50,
    ) -> tuple[ChatActionPlanRecord, ...]:
        """Return pending action plans for a thread scoped to an entity.

        Both thread_id and entity_id are required so callers cannot enumerate
        action plans from threads in workspaces they do not belong to.
        """

        statement = (
            select(ChatActionPlan)
            .where(
                ChatActionPlan.thread_id == thread_id,
                ChatActionPlan.entity_id == entity_id,
                ChatActionPlan.status == "pending",
            )
            .order_by(desc(ChatActionPlan.created_at))
            .limit(limit)
        )
        plans = self._db_session.execute(statement).scalars().all()
        return tuple(_map_action_plan(plan) for plan in plans)

    def list_actions_for_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        limit: int = 50,
    ) -> tuple[ChatActionPlanRecord, ...]:
        """Return recent action plans for one thread in newest-first order."""

        statement = (
            select(ChatActionPlan)
            .where(
                ChatActionPlan.thread_id == thread_id,
                ChatActionPlan.entity_id == entity_id,
            )
            .order_by(desc(ChatActionPlan.created_at))
            .limit(limit)
        )
        plans = self._db_session.execute(statement).scalars().all()
        return tuple(_map_action_plan(plan) for plan in plans)

    def list_actions_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
        status: str | None = None,
        limit: int = 50,
    ) -> tuple[ChatActionPlanRecord, ...]:
        """Return action plans targeting a specific business object."""

        statement = select(ChatActionPlan).where(
            ChatActionPlan.target_type == target_type,
            ChatActionPlan.target_id == target_id,
        )
        if status is not None:
            statement = statement.where(ChatActionPlan.status == status)
        statement = statement.order_by(desc(ChatActionPlan.created_at)).limit(limit)
        plans = self._db_session.execute(statement).scalars().all()
        return tuple(_map_action_plan(plan) for plan in plans)

    def update_action_plan_status(
        self,
        *,
        action_plan_id: UUID,
        status: str,
        applied_result: dict[str, Any] | None = None,
        rejected_reason: str | None = None,
        superseded_by_id: UUID | None = None,
    ) -> ChatActionPlanRecord | None:
        """Transition an action plan to a new review status."""

        statement = select(ChatActionPlan).where(ChatActionPlan.id == action_plan_id)
        plan = self._db_session.execute(statement).scalar_one_or_none()
        if plan is None:
            return None

        plan.status = status
        if applied_result is not None:
            plan.applied_result = applied_result
        if rejected_reason is not None:
            plan.rejected_reason = rejected_reason
        if superseded_by_id is not None:
            plan.superseded_by_id = superseded_by_id

        self._db_session.flush()
        return _map_action_plan(plan)

    def supersede_pending_actions_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
        superseded_by_id: UUID,
    ) -> int:
        """Mark pending actions for a target as superseded by a newer action."""

        statement = (
            select(ChatActionPlan)
            .where(
                ChatActionPlan.target_type == target_type,
                ChatActionPlan.target_id == target_id,
                ChatActionPlan.status == "pending",
            )
        )
        plans = self._db_session.execute(statement).scalars().all()
        count = 0
        for plan in plans:
            plan.status = "superseded"
            plan.superseded_by_id = superseded_by_id
            count += 1
        if count > 0:
            self._db_session.flush()
        return count

    def commit(self) -> None:
        """Commit the current transaction."""
        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self._db_session.rollback()


def _map_action_plan(model: ChatActionPlan) -> ChatActionPlanRecord:
    """Convert an ORM chat action plan model into the immutable record consumed by services."""

    return ChatActionPlanRecord(
        id=model.id,
        thread_id=model.thread_id,
        message_id=model.message_id,
        entity_id=model.entity_id,
        close_run_id=model.close_run_id,
        actor_user_id=model.actor_user_id,
        intent=model.intent,
        target_type=model.target_type,
        target_id=model.target_id,
        payload=model.payload,
        confidence=model.confidence,
        autonomy_mode=model.autonomy_mode,
        status=model.status,
        requires_human_approval=model.requires_human_approval,
        reasoning=model.reasoning,
        applied_result=model.applied_result,
        rejected_reason=model.rejected_reason,
        superseded_by_id=model.superseded_by_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


__all__ = [
    "ChatActionPlanRecord",
    "ChatActionRepository",
]
