"""
Purpose: Provide the generic planning and execution kernel for tool-aware
agent runtimes.
Scope: Structured planning, tool validation, target resolution, prompt
assembly, and deterministic tool dispatch.
Dependencies: Model gateway, registry, execution policy, and agent contracts.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from services.agents.models import (
    AgentExecutionContext,
    AgentPlannedAction,
    AgentPlanningResult,
)
from services.agents.policy import ExecutionPolicy
from services.agents.registry import ToolRegistry, ToolRegistryError
from services.model_gateway.client import ModelGateway, ModelGatewayError

_READ_ONLY_TOOL_NAME = "answer_operator"
_ASSISTANT_RESPONSE_FIELD = "assistant_response"
_REASONING_FIELD = "reasoning"


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
                (
                    "Native planning protocol: call exactly one tool. Use "
                    f"{_READ_ONLY_TOOL_NAME} when the best response is read-only. "
                    "Use a concrete platform tool when the operator's request is "
                    "actionable. Every tool call must include assistant_response "
                    "and reasoning in its arguments."
                ),
                "Available tools:",
                *self._tool_registry.describe_tools_for_prompt(),
                "Current workspace snapshot follows as JSON.",
                json.dumps(snapshot, default=str),
            ]
        )
        try:
            tool_call = self._model_gateway.complete_tool_call(
                messages=[{"role": "system", "content": system_prompt}, *conversation],
                tools=_build_native_planning_tools(self._tool_registry),
            )
        except ModelGatewayError as error:
            raise AgentKernelError(str(error)) from error
        return _coerce_tool_call_to_planning_result(
            tool_name=tool_call.name,
            tool_arguments=tool_call.arguments,
        )

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


def _build_native_planning_tools(tool_registry: ToolRegistry) -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for agent planning."""

    tools = [_build_read_only_tool()]
    tools.extend(_build_platform_tool(tool) for tool in tool_registry.list_tools())
    return tools


def _build_read_only_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _READ_ONLY_TOOL_NAME,
            "description": (
                "Use this when the operator needs an explanation, status update, "
                "clarifying question, or any response that should not mutate state."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": _planning_metadata_properties(),
                "required": [_ASSISTANT_RESPONSE_FIELD, _REASONING_FIELD],
            },
        },
    }


def _build_platform_tool(tool: Any) -> dict[str, Any]:
    schema = deepcopy(tool.input_schema)
    properties = schema.setdefault("properties", {})
    if not isinstance(properties, dict):
        raise AgentKernelError(f"Tool '{tool.name}' has invalid schema properties.")
    properties.update(_planning_metadata_properties())
    required = schema.setdefault("required", [])
    if not isinstance(required, list):
        raise AgentKernelError(f"Tool '{tool.name}' has invalid schema requirements.")
    for field_name in (_ASSISTANT_RESPONSE_FIELD, _REASONING_FIELD):
        if field_name not in required:
            required.append(field_name)
    schema["additionalProperties"] = False
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (
                f"{tool.description} Include a brief operator-facing "
                "assistant_response and concise audit reasoning."
            ),
            "parameters": schema,
        },
    }


def _planning_metadata_properties() -> dict[str, Any]:
    return {
        _ASSISTANT_RESPONSE_FIELD: {
            "type": "string",
            "minLength": 1,
            "description": "Brief operator-facing response shown in chat.",
        },
        _REASONING_FIELD: {
            "type": "string",
            "minLength": 1,
            "maxLength": 3000,
            "description": "Concise private audit reasoning for the selected action.",
        },
    }


def _coerce_tool_call_to_planning_result(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
) -> AgentPlanningResult:
    arguments = dict(tool_arguments)
    assistant_response = arguments.pop(_ASSISTANT_RESPONSE_FIELD, None)
    reasoning = arguments.pop(_REASONING_FIELD, None)
    if not isinstance(assistant_response, str) or not assistant_response.strip():
        raise AgentKernelError("Model tool call omitted assistant_response.")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise AgentKernelError("Model tool call omitted reasoning.")
    if tool_name == _READ_ONLY_TOOL_NAME:
        return AgentPlanningResult(
            mode="read_only",
            assistant_response=assistant_response,
            reasoning=reasoning,
            tool_name=None,
            tool_arguments={},
        )
    return AgentPlanningResult(
        mode="tool",
        assistant_response=assistant_response,
        reasoning=reasoning,
        tool_name=tool_name,
        tool_arguments=arguments,
    )
