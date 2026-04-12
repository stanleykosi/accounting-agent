"""
Purpose: Implement the LangGraph-based recommendation workflow that orchestrates
deterministic rules, model reasoning, and autonomy routing into validated recommendations.
Scope: Defines the canonical state machine graph used by the Celery recommendation task.
Dependencies: langgraph (StateGraph, START, END), orchestration nodes, graph state contract.

Design notes:
- The graph is the canonical workflow for generating accounting recommendations.
- It enforces: validate → deterministic rules → conditional model call → assemble → route.
- The graph itself does NOT persist anything. The calling worker task handles persistence.
- Every edge is explicit. There are no silent fallbacks or automatic retries inside the graph.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from services.common.logging import get_logger
from services.orchestration.nodes import (
    apply_autonomy_routing,
    assemble_recommendation,
    evaluate_deterministic_rules,
    invoke_model_reasoning,
    should_invoke_model,
    validate_prerequisites,
)

logger = get_logger(__name__)


class RecommendationGraphError(Exception):
    """Represent a hard failure in the recommendation graph construction or execution."""


def build_recommendation_graph() -> Any:
    """Construct the canonical LangGraph recommendation state machine.

    The graph topology:
        START → validate_prerequisites → evaluate_deterministic_rules
            → (conditional: should_invoke_model)
                → invoke_model_reasoning → assemble_recommendation
                → assemble_recommendation (skip model when high-confidence deterministic result)
            → apply_autonomy_routing → END

    Returns:
        Compiled LangGraph StateGraph ready for invocation.
    """
    # Use dict as the raw state type; GraphState provides the validation schema
    graph = StateGraph(dict)  # type: ignore[type-var]

    # Register all nodes
    graph.add_node("validate_prerequisites", validate_prerequisites)  # type: ignore[type-var]
    graph.add_node("evaluate_deterministic_rules", evaluate_deterministic_rules)  # type: ignore[type-var]
    graph.add_node("invoke_model_reasoning", invoke_model_reasoning)  # type: ignore[type-var]
    graph.add_node("assemble_recommendation", assemble_recommendation)  # type: ignore[type-var]
    graph.add_node("apply_autonomy_routing", apply_autonomy_routing)  # type: ignore[type-var]

    # Define the linear entry path
    graph.add_edge(START, "validate_prerequisites")
    graph.add_edge("validate_prerequisites", "evaluate_deterministic_rules")

    # Conditional branching after deterministic rule evaluation
    graph.add_conditional_edges(
        "evaluate_deterministic_rules",
        should_invoke_model,
        {
            "invoke_model_reasoning": "invoke_model_reasoning",
            "assemble_recommendation": "assemble_recommendation",
        },
    )

    # Model reasoning flows into assembly
    graph.add_edge("invoke_model_reasoning", "assemble_recommendation")

    # Assembly flows into autonomy routing
    graph.add_edge("assemble_recommendation", "apply_autonomy_routing")

    # Autonomy routing is the terminal node
    graph.add_edge("apply_autonomy_routing", END)

    return graph


def execute_recommendation_workflow(
    context: dict[str, Any],
) -> dict[str, Any]:
    """Execute the full recommendation workflow for a given context.

    This is the primary entry point for the Celery task. It:
    1. Builds and compiles the graph
    2. Invokes it with the initial context
    3. Returns the final state for downstream persistence

    Args:
        context: RecommendationContext data as a dict (from the Celery task).

    Returns:
        Final graph state dict containing the assembled recommendation or errors.

    Raises:
        RecommendationGraphError: When the graph execution fails.
    """
    try:
        workflow = build_recommendation_graph().compile()
    except Exception as error:
        raise RecommendationGraphError(
            f"Failed to compile recommendation graph: {error}"
        ) from error

    initial_state: dict[str, Any] = {
        "context": context,
        "deterministic_result": None,
        "model_reasoning": None,
        "final_recommendation": None,
        "errors": [],
    }

    try:
        final_state = workflow.invoke(initial_state)
    except Exception as error:
        logger.error("recommendation_graph_execution_failed", error=str(error))
        raise RecommendationGraphError(
            f"Recommendation graph execution failed: {error}"
        ) from error

    errors = final_state.get("errors", [])
    if errors:
        logger.warning(
            "recommendation_graph_completed_with_errors",
            errors=errors,
        )

    return final_state  # type: ignore[no-any-return]


__all__ = [
    "RecommendationGraphError",
    "build_recommendation_graph",
    "execute_recommendation_workflow",
]
