"""
Purpose: Verify the agent kernel's native tool-call planning contract.
Scope: Read-only pseudo-tool coercion, platform tool argument stripping, and
native schema generation for registered deterministic tools.
Dependencies: AgentKernel, ToolRegistry, ExecutionPolicy, and lightweight gateway doubles.
"""

from __future__ import annotations

from typing import Any

from services.agents.kernel import AgentKernel
from services.agents.models import AgentExecutionContext, AgentToolDefinition
from services.agents.policy import ExecutionPolicy
from services.agents.registry import ToolRegistry
from services.model_gateway.client import ModelGatewayToolCall


class FakeModelGateway:
    def __init__(self, tool_call: ModelGatewayToolCall) -> None:
        self.tool_call = tool_call
        self.calls: list[dict[str, Any]] = []

    def complete_tool_call(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ModelGatewayToolCall:
        self.calls.append({"messages": messages, "tools": tools})
        return self.tool_call


def test_agent_kernel_uses_native_read_only_tool_for_direct_answers() -> None:
    gateway = FakeModelGateway(
        ModelGatewayToolCall(
            name="answer_operator",
            arguments={
                "assistant_response": "The close run is waiting on document review.",
                "reasoning": "The operator asked for status only.",
            },
            content="",
        )
    )
    registry = _build_registry()
    kernel = AgentKernel(
        model_gateway=gateway,  # type: ignore[arg-type]
        tool_registry=registry,
        execution_policy=ExecutionPolicy(tool_registry=registry),
    )

    planning = kernel.plan(
        instructions="You are an accounting agent.",
        conversation=[{"role": "user", "content": "where are we?"}],
        snapshot={"progress_summary": "Document review pending."},
    )

    assert planning.mode == "read_only"
    assert planning.tool_name is None
    assert planning.tool_arguments == {}
    assert planning.assistant_response == "The close run is waiting on document review."
    native_tool_names = {
        tool["function"]["name"]
        for tool in gateway.calls[0]["tools"]
        if isinstance(tool.get("function"), dict)
    }
    assert native_tool_names == {"answer_operator", "create_workspace"}


def test_agent_kernel_strips_planning_metadata_before_tool_validation() -> None:
    gateway = FakeModelGateway(
        ModelGatewayToolCall(
            name="create_workspace",
            arguments={
                "assistant_response": "I'll create that workspace now.",
                "reasoning": "The operator provided the required workspace name.",
                "name": "Stanley",
                "legal_name": "Stanley Limited",
            },
            content="",
        )
    )
    registry = _build_registry()
    kernel = AgentKernel(
        model_gateway=gateway,  # type: ignore[arg-type]
        tool_registry=registry,
        execution_policy=ExecutionPolicy(tool_registry=registry),
    )

    planning = kernel.plan(
        instructions="You are an accounting agent.",
        conversation=[{"role": "user", "content": "create workspace Stanley"}],
        snapshot={},
    )
    action = kernel.resolve_action(planning=planning)

    assert action is not None
    assert planning.mode == "tool"
    assert planning.tool_name == "create_workspace"
    assert planning.tool_arguments == {
        "name": "Stanley",
        "legal_name": "Stanley Limited",
    }
    native_tool = next(
        tool
        for tool in gateway.calls[0]["tools"]
        if tool["function"]["name"] == "create_workspace"
    )
    parameters = native_tool["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert "assistant_response" in parameters["required"]
    assert "reasoning" in parameters["required"]
    assert "name" in parameters["required"]
    assert "legal_name" in parameters["required"]


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_tool(
        definition=AgentToolDefinition(
            name="create_workspace",
            namespace="workspace_control",
            namespace_label="Workspace Control",
            specialist_name="Workspace specialist",
            specialist_mission="Manage workspaces.",
            prompt_signature="create_workspace(name, legal_name)",
            description="Create a workspace after required details are known.",
            intent="proposed_edit",
            requires_human_approval=False,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "legal_name": {"type": "string", "minLength": 1},
                },
                "required": ["name", "legal_name"],
            },
        ),
        executor=_execute_noop,
    )
    return registry


def _execute_noop(
    arguments: dict[str, Any],
    context: AgentExecutionContext,
) -> dict[str, Any]:
    return {"arguments": arguments, "entity_id": str(context.entity_id)}
