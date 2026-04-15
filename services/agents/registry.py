"""
Purpose: Register deterministic tools for the agent runtime and expose uniform
lookup, prompt rendering, target derivation, and execution.
Scope: Tool metadata registration, executor dispatch, and prompt signatures.
Dependencies: Agent model contracts and Python callables only.
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from services.agents.models import AgentExecutionContext, AgentToolDefinition

ToolExecutor = Callable[[dict[str, Any], AgentExecutionContext], dict[str, Any]]
TargetDeriver = Callable[[dict[str, Any]], tuple[str | None, UUID | None]]


class ToolRegistryError(Exception):
    """Represent an invalid registry access or tool execution binding."""


class ToolRegistry:
    """Store deterministic tools and their execution bindings."""

    def __init__(self) -> None:
        self._definitions: dict[str, AgentToolDefinition] = {}
        self._executors: dict[str, ToolExecutor] = {}
        self._target_derivers: dict[str, TargetDeriver] = {}

    def register_tool(
        self,
        *,
        definition: AgentToolDefinition,
        executor: ToolExecutor,
        target_deriver: TargetDeriver | None = None,
    ) -> None:
        """Register one tool definition and its bound executor."""

        if definition.name in self._definitions:
            raise ToolRegistryError(f"Tool '{definition.name}' is already registered.")
        try:
            Draft202012Validator.check_schema(definition.input_schema)
        except SchemaError as error:
            raise ToolRegistryError(
                f"Tool '{definition.name}' has an invalid input schema: {error.message}"
            ) from error
        self._definitions[definition.name] = definition
        self._executors[definition.name] = executor
        if target_deriver is not None:
            self._target_derivers[definition.name] = target_deriver

    def get_tool(self, *, tool_name: str) -> AgentToolDefinition:
        """Return one registered tool definition or fail fast."""

        tool = self._definitions.get(tool_name)
        if tool is None:
            raise ToolRegistryError(f"Tool '{tool_name}' is not registered.")
        return tool

    def describe_tools_for_prompt(self) -> tuple[str, ...]:
        """Return prompt-ready tool signature lines in registration order."""

        return tuple(f"- {tool.prompt_signature}" for tool in self._definitions.values())

    def list_tools(self) -> tuple[AgentToolDefinition, ...]:
        """Return registered tool definitions in registration order."""

        return tuple(self._definitions.values())

    def derive_target(
        self,
        *,
        tool_name: str,
        tool_arguments: dict[str, Any],
    ) -> tuple[str | None, UUID | None]:
        """Return target metadata for one tool invocation when configured."""

        self.validate_arguments(tool_name=tool_name, tool_arguments=tool_arguments)
        deriver = self._target_derivers.get(tool_name)
        if deriver is None:
            return None, None
        return deriver(tool_arguments)

    def validate_arguments(
        self,
        *,
        tool_name: str,
        tool_arguments: dict[str, Any],
    ) -> None:
        """Validate one tool argument payload against the registered JSON schema."""

        tool = self.get_tool(tool_name=tool_name)
        try:
            Draft202012Validator(tool.input_schema).validate(tool_arguments)
        except ValidationError as error:
            path = ".".join(str(segment) for segment in error.absolute_path)
            location = f" at '{path}'" if path else ""
            raise ToolRegistryError(
                f"Tool '{tool_name}' received invalid arguments{location}: {error.message}"
            ) from error

    def execute(
        self,
        *,
        tool_name: str,
        tool_arguments: dict[str, Any],
        execution_context: AgentExecutionContext,
    ) -> dict[str, Any]:
        """Dispatch one tool invocation through its registered executor."""

        executor = self._executors.get(tool_name)
        if executor is None:
            raise ToolRegistryError(f"Tool '{tool_name}' is not registered.")
        self.validate_arguments(tool_name=tool_name, tool_arguments=tool_arguments)
        return executor(tool_arguments, execution_context)
