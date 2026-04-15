"""
Purpose: Define generic agent runtime contracts shared by planners, registries,
policies, and execution adapters.
Scope: Structured planning output, tool metadata, execution context, and
planned-action resolution.
Dependencies: Shared contract model base plus Python dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from services.contracts.api_models import ContractModel


class AgentPlanningResult(ContractModel):
    """Capture the planner's decision to answer directly or invoke one tool."""

    mode: Literal["read_only", "tool"] = Field(description="Whether to answer directly or call a tool.")
    assistant_response: str = Field(
        min_length=1,
        description="Assistant response shown to the operator.",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=3000,
        description="Planner reasoning retained for audit and diagnosis.",
    )
    tool_name: str | None = Field(
        default=None,
        min_length=1,
        description="Registered tool selected when mode is tool.",
    )
    tool_arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-safe argument payload for the selected tool.",
    )


@dataclass(frozen=True, slots=True)
class AgentToolDefinition:
    """Describe one registered deterministic tool exposed to the agent planner."""

    name: str
    prompt_signature: str
    description: str
    intent: str
    requires_human_approval: bool
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    """Carry the runtime context passed into deterministic tool execution."""

    actor: Any
    entity_id: UUID
    close_run_id: UUID | None
    thread_id: UUID | None
    trace_id: str | None
    source_surface: Any


@dataclass(frozen=True, slots=True)
class AgentPlannedAction:
    """Describe one resolved planned action after planner output validation."""

    planning: AgentPlanningResult
    tool: AgentToolDefinition
    target_type: str | None
    target_id: UUID | None
