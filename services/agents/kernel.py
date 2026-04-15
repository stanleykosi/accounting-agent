"""
Purpose: Provide the generic planning and execution kernel for tool-aware
agent runtimes.
Scope: Structured planning, tool validation, target resolution, prompt
assembly, and deterministic tool dispatch.
Dependencies: Model gateway, registry, execution policy, and agent contracts.
"""

from __future__ import annotations

import json
from typing import Any

from services.agents.models import (
    AgentExecutionContext,
    AgentPlannedAction,
    AgentPlanningResult,
)
from services.agents.policy import ExecutionPolicy
from services.agents.registry import ToolRegistry, ToolRegistryError
from services.model_gateway.client import ModelGateway, ModelGatewayError


class AgentKernelError(Exception):
    """Represent an expected planning or execution failure in the agent kernel."""


class AgentKernel:
    """Coordinate planning and execution against one registered tool registry."""

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_registry: ToolRegistry,
        execution_policy: ExecutionPolicy,
    ) -> None:
        self._model_gateway = model_gateway
        self._tool_registry = tool_registry
        self._execution_policy = execution_policy

    def plan(
        self,
        *,
        instructions: str,
        conversation: list[dict[str, str]],
        snapshot: dict[str, Any],
    ) -> AgentPlanningResult:
        """Run structured planning against the live workspace snapshot."""

        system_prompt = "\n".join(
            [
                instructions,
                "Available tools:",
                *self._tool_registry.describe_tools_for_prompt(),
                "Current workspace snapshot follows as JSON.",
                json.dumps(snapshot, default=str),
            ]
        )
        try:
            return self._model_gateway.complete_structured(
                messages=[{"role": "system", "content": system_prompt}, *conversation],
                response_model=AgentPlanningResult,
            )
        except ModelGatewayError as error:
            raise AgentKernelError(str(error)) from error

    def resolve_action(
        self,
        *,
        planning: AgentPlanningResult,
    ) -> AgentPlannedAction | None:
        """Validate the planner-selected tool and resolve metadata for staging."""

        if planning.mode == "read_only" or planning.tool_name is None:
            return None
        try:
            tool = self._tool_registry.get_tool(tool_name=planning.tool_name)
            target_type, target_id = self._tool_registry.derive_target(
                tool_name=planning.tool_name,
                tool_arguments=planning.tool_arguments,
            )
        except ToolRegistryError as error:
            raise AgentKernelError(str(error)) from error
        return AgentPlannedAction(
            planning=planning,
            tool=tool,
            target_type=target_type,
            target_id=target_id,
        )

    def requires_human_approval(self, *, action: AgentPlannedAction) -> bool:
        """Return whether one resolved action must stage for approval."""

        return self._execution_policy.requires_human_approval(tool_name=action.tool.name)

    def execute(
        self,
        *,
        action: AgentPlannedAction,
        execution_context: AgentExecutionContext,
    ) -> dict[str, Any]:
        """Execute one resolved action through the registered deterministic tool."""

        try:
            return self._tool_registry.execute(
                tool_name=action.tool.name,
                tool_arguments=action.planning.tool_arguments,
                execution_context=execution_context,
            )
        except ToolRegistryError as error:
            raise AgentKernelError(str(error)) from error
