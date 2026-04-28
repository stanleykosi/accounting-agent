"""
Purpose: Provide a reusable execution-policy layer for agent tool invocations.
Scope: Human-approval requirements and execution-policy decisions.
Dependencies: The tool registry only.
"""

from __future__ import annotations

from services.agents.registry import ToolRegistry


class ExecutionPolicy:
    """Resolve whether a registered tool may execute immediately or needs approval."""

    def __init__(self, *, tool_registry: ToolRegistry) -> None:
        self._tool_registry = tool_registry

    def requires_human_approval(self, *, tool_name: str) -> bool:
        """Return whether one tool invocation must stage for approval."""

        return self._tool_registry.get_tool(tool_name=tool_name).requires_human_approval
