"""
Purpose: Provide the canonical operator-memory helpers shared by chat thread
creation, workspace reads, and the operator runtime.
Scope: Cross-thread preference carry-forward, compact recent-value handling,
and stable extraction of operator-memory snapshots from chat thread context.
Dependencies: Shared JSON types plus UTC timestamp helpers only.
"""

from __future__ import annotations

from typing import Any

from services.common.types import utc_now

DEFAULT_PREFERRED_EXPLANATION_DEPTH = "balanced"
DEFAULT_PREFERRED_CONFIRMATION_STYLE = "confirm_high_risk"


def compact_recent_values(values: list[object], *, limit: int) -> tuple[str, ...]:
    """Return a compact deduplicated tail of recent string values."""

    compact: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if cleaned in compact:
            compact.remove(cleaned)
        compact.append(cleaned)
    return tuple(compact[-limit:])


def optional_memory_text(payload: dict[str, Any] | None, key: str) -> str | None:
    """Return one trimmed string field from a memory or async payload when present."""

    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def optional_memory_int(payload: dict[str, Any] | None, key: str) -> int:
    """Return one non-negative integer field from a memory or async payload when present."""

    if not isinstance(payload, dict):
        return 0
    value = payload.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def build_cross_thread_memory_seed(
    *,
    recent_context_payloads: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Return the cross-thread operator-memory values that should carry forward."""

    preferred_explanation_depth = DEFAULT_PREFERRED_EXPLANATION_DEPTH
    preferred_confirmation_style = DEFAULT_PREFERRED_CONFIRMATION_STYLE
    recent_tool_names: list[object] = []
    recent_tool_namespaces: list[object] = []
    recent_objectives: list[object] = []
    recent_entity_names: list[object] = []
    recent_period_labels: list[object] = []
    recent_target_labels: list[object] = []
    working_subtask: str | None = None
    approved_objective: str | None = None
    pending_branch: str | None = None
    last_async_status: str | None = None
    last_async_objective: str | None = None
    last_async_note: str | None = None

    for payload in recent_context_payloads:
        snapshot = extract_operator_memory_snapshot(context_payload=payload)
        snapshot_explanation_depth = snapshot["preferred_explanation_depth"]
        if (
            preferred_explanation_depth == DEFAULT_PREFERRED_EXPLANATION_DEPTH
            and isinstance(snapshot_explanation_depth, str)
            and snapshot_explanation_depth != DEFAULT_PREFERRED_EXPLANATION_DEPTH
        ):
            preferred_explanation_depth = snapshot_explanation_depth

        snapshot_confirmation_style = snapshot["preferred_confirmation_style"]
        if (
            preferred_confirmation_style == DEFAULT_PREFERRED_CONFIRMATION_STYLE
            and isinstance(snapshot_confirmation_style, str)
            and snapshot_confirmation_style != DEFAULT_PREFERRED_CONFIRMATION_STYLE
        ):
            preferred_confirmation_style = snapshot_confirmation_style

        recent_tool_names.extend(snapshot["recent_tool_names"])
        recent_tool_namespaces.extend(snapshot["recent_tool_namespaces"])
        recent_objectives.extend(snapshot["recent_objectives"])
        recent_entity_names.extend(snapshot["recent_entity_names"])
        recent_period_labels.extend(snapshot["recent_period_labels"])
        recent_target_labels.extend(snapshot["recent_target_labels"])
        if working_subtask is None and isinstance(snapshot["working_subtask"], str):
            working_subtask = snapshot["working_subtask"]
        if approved_objective is None and isinstance(snapshot["approved_objective"], str):
            approved_objective = snapshot["approved_objective"]
        if pending_branch is None and isinstance(snapshot["pending_branch"], str):
            pending_branch = snapshot["pending_branch"]

        if last_async_status is None:
            snapshot_last_async_status = snapshot["last_async_status"]
            if isinstance(snapshot_last_async_status, str) and snapshot_last_async_status.strip():
                last_async_status = snapshot_last_async_status
                last_async_objective = snapshot["last_async_objective"]
                last_async_note = snapshot["last_async_note"]

    return {
        "preferred_explanation_depth": preferred_explanation_depth,
        "preferred_confirmation_style": preferred_confirmation_style,
        "recent_tool_names": compact_recent_values(recent_tool_names, limit=5),
        "recent_tool_namespaces": compact_recent_values(recent_tool_namespaces, limit=5),
        "recent_objectives": compact_recent_values(recent_objectives, limit=4),
        "recent_entity_names": compact_recent_values(recent_entity_names, limit=4),
        "recent_period_labels": compact_recent_values(recent_period_labels, limit=4),
        "recent_target_labels": compact_recent_values(recent_target_labels, limit=5),
        "working_subtask": working_subtask,
        "approved_objective": approved_objective,
        "pending_branch": pending_branch,
        "last_async_status": last_async_status,
        "last_async_objective": last_async_objective,
        "last_async_note": last_async_note,
    }


def build_cross_workspace_preference_seed(
    *,
    recent_context_payloads: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Return user-wide operator preferences that should carry across workspaces."""

    preferred_explanation_depth = DEFAULT_PREFERRED_EXPLANATION_DEPTH
    preferred_confirmation_style = DEFAULT_PREFERRED_CONFIRMATION_STYLE
    recent_tool_names: list[object] = []
    recent_tool_namespaces: list[object] = []

    for payload in recent_context_payloads:
        snapshot = extract_operator_memory_snapshot(context_payload=payload)
        snapshot_explanation_depth = snapshot["preferred_explanation_depth"]
        if (
            preferred_explanation_depth == DEFAULT_PREFERRED_EXPLANATION_DEPTH
            and isinstance(snapshot_explanation_depth, str)
            and snapshot_explanation_depth != DEFAULT_PREFERRED_EXPLANATION_DEPTH
        ):
            preferred_explanation_depth = snapshot_explanation_depth

        snapshot_confirmation_style = snapshot["preferred_confirmation_style"]
        if (
            preferred_confirmation_style == DEFAULT_PREFERRED_CONFIRMATION_STYLE
            and isinstance(snapshot_confirmation_style, str)
            and snapshot_confirmation_style != DEFAULT_PREFERRED_CONFIRMATION_STYLE
        ):
            preferred_confirmation_style = snapshot_confirmation_style

        recent_tool_names.extend(snapshot["recent_tool_names"])
        recent_tool_namespaces.extend(snapshot["recent_tool_namespaces"])

    return {
        "preferred_explanation_depth": preferred_explanation_depth,
        "preferred_confirmation_style": preferred_confirmation_style,
        "recent_tool_names": compact_recent_values(recent_tool_names, limit=5),
        "recent_tool_namespaces": compact_recent_values(recent_tool_namespaces, limit=5),
    }


def _memory_tuple(
    context_payload: dict[str, Any],
    memory: dict[str, Any],
    top_level_key: str,
    nested_key: str,
) -> tuple[str, ...]:
    """Return one compact tuple from top-level context or nested agent memory."""

    top_level_value = context_payload.get(top_level_key)
    if isinstance(top_level_value, tuple):
        return tuple(item for item in top_level_value if isinstance(item, str))
    if isinstance(top_level_value, list):
        return tuple(item for item in top_level_value if isinstance(item, str))

    nested_value = memory.get(nested_key)
    if isinstance(nested_value, tuple):
        return tuple(item for item in nested_value if isinstance(item, str))
    if isinstance(nested_value, list):
        return tuple(item for item in nested_value if isinstance(item, str))
    return ()


def extract_operator_memory_snapshot(
    *,
    context_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the operator-memory snapshot encoded in one thread context payload."""

    memory = (
        dict(context_payload.get("agent_memory"))
        if isinstance(context_payload.get("agent_memory"), dict)
        else {}
    )
    last_async_turn = (
        dict(context_payload.get("agent_last_async_turn"))
        if isinstance(context_payload.get("agent_last_async_turn"), dict)
        else None
    )
    return {
        "preferred_explanation_depth": (
            memory.get("preferred_explanation_depth")
            if isinstance(memory.get("preferred_explanation_depth"), str)
            else DEFAULT_PREFERRED_EXPLANATION_DEPTH
        ),
        "preferred_confirmation_style": (
            memory.get("preferred_confirmation_style")
            if isinstance(memory.get("preferred_confirmation_style"), str)
            else DEFAULT_PREFERRED_CONFIRMATION_STYLE
        ),
        "recent_tool_names": _memory_tuple(
            context_payload,
            memory,
            "agent_recent_tool_names",
            "recent_tool_names",
        ),
        "recent_tool_namespaces": _memory_tuple(
            context_payload,
            memory,
            "agent_recent_tool_namespaces",
            "recent_tool_namespaces",
        ),
        "recent_objectives": _memory_tuple(
            context_payload,
            memory,
            "agent_recent_objectives",
            "recent_objectives",
        ),
        "recent_entity_names": _memory_tuple(
            context_payload,
            memory,
            "agent_recent_entity_names",
            "recent_entity_names",
        ),
        "recent_period_labels": _memory_tuple(
            context_payload,
            memory,
            "agent_recent_period_labels",
            "recent_period_labels",
        ),
        "recent_target_labels": _memory_tuple(
            context_payload,
            memory,
            "agent_recent_target_labels",
            "recent_target_labels",
        ),
        "last_target_type": (
            memory.get("last_target_type")
            if isinstance(memory.get("last_target_type"), str)
            else None
        ),
        "last_target_id": (
            memory.get("last_target_id")
            if isinstance(memory.get("last_target_id"), str)
            else None
        ),
        "last_target_label": (
            memory.get("last_target_label")
            if isinstance(memory.get("last_target_label"), str)
            else None
        ),
        "working_subtask": (
            memory.get("working_subtask")
            if isinstance(memory.get("working_subtask"), str)
            else None
        ),
        "approved_objective": (
            memory.get("approved_objective")
            if isinstance(memory.get("approved_objective"), str)
            else None
        ),
        "pending_branch": (
            memory.get("pending_branch")
            if isinstance(memory.get("pending_branch"), str)
            else None
        ),
        "last_async_status": optional_memory_text(last_async_turn, "status"),
        "last_async_objective": optional_memory_text(last_async_turn, "objective"),
        "last_async_note": optional_memory_text(last_async_turn, "final_note"),
    }


def build_recovery_guidance(
    *,
    active_async_turn: dict[str, Any] | None,
    last_async_turn: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return operator-facing recovery state derived from async workflow context."""

    active_status = optional_memory_text(active_async_turn, "status")
    active_objective = optional_memory_text(active_async_turn, "objective")
    active_retry_count = optional_memory_int(active_async_turn, "resume_attempt_count")
    active_last_failure = optional_memory_text(active_async_turn, "last_resume_failure")

    if active_status is not None:
        objective_label = active_objective or "the current workflow"
        if active_last_failure is not None:
            actions = [
                "Retry the request in chat after confirming the worker is healthy.",
                "Inspect recent traces if the same recovery failure repeats.",
            ]
            if active_retry_count > 1:
                actions.append(
                    "If retries keep failing, ask the assistant to restart the workflow "
                    "cleanly."
                )
            return {
                "recovery_state": "attention_required",
                "recovery_summary": (
                    f"{objective_label} is waiting to resume after a recovery issue: "
                    f"{active_last_failure}"
                ),
                "recovery_actions": tuple(actions),
            }
        if active_status == "resuming":
            return {
                "recovery_state": "resuming",
                "recovery_summary": (
                    f"{objective_label} is resuming after background work finished."
                ),
                "recovery_actions": (
                    "Stay in this thread while the assistant continues automatically.",
                ),
            }
        return {
            "recovery_state": "working",
            "recovery_summary": f"{objective_label} is still running in the background.",
            "recovery_actions": (
                "Wait here for the assistant to continue automatically when the jobs finish.",
            ),
        }

    last_status = optional_memory_text(last_async_turn, "status")
    last_objective = optional_memory_text(last_async_turn, "objective") or "the last workflow"
    last_note = optional_memory_text(last_async_turn, "final_note")
    if last_status == "blocked":
        return {
            "recovery_state": "attention_required",
            "recovery_summary": (
                f"{last_objective} stopped because it needs operator intervention."
                + (f" {last_note}" if last_note is not None else "")
            ),
            "recovery_actions": (
                "Resolve the blocker the assistant reported, then ask it to continue.",
            ),
        }
    if last_status == "failed":
        return {
            "recovery_state": "attention_required",
            "recovery_summary": (
                f"{last_objective} failed in background processing."
                + (f" {last_note}" if last_note is not None else "")
            ),
            "recovery_actions": (
                "Retry the workflow in chat after checking worker health and recent traces.",
            ),
        }
    if last_status == "canceled":
        return {
            "recovery_state": "paused",
            "recovery_summary": f"{last_objective} was canceled before completion.",
            "recovery_actions": (
                "Ask the assistant to restart the workflow when you're ready to continue.",
            ),
        }

    return {
        "recovery_state": None,
        "recovery_summary": None,
        "recovery_actions": (),
    }


def seed_context_payload_with_operator_memory(
    *,
    context_payload: dict[str, Any],
    recent_context_payloads: tuple[dict[str, Any], ...],
    cross_workspace_recent_context_payloads: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Return a thread context payload seeded with cross-thread operator memory."""

    seed = build_cross_thread_memory_seed(recent_context_payloads=recent_context_payloads)
    cross_workspace_preferences = build_cross_workspace_preference_seed(
        recent_context_payloads=cross_workspace_recent_context_payloads
    )
    seed["preferred_explanation_depth"] = (
        seed["preferred_explanation_depth"]
        if seed["preferred_explanation_depth"] != DEFAULT_PREFERRED_EXPLANATION_DEPTH
        else cross_workspace_preferences["preferred_explanation_depth"]
    )
    seed["preferred_confirmation_style"] = (
        seed["preferred_confirmation_style"]
        if seed["preferred_confirmation_style"] != DEFAULT_PREFERRED_CONFIRMATION_STYLE
        else cross_workspace_preferences["preferred_confirmation_style"]
    )
    seed["recent_tool_names"] = compact_recent_values(
        [
            *cross_workspace_preferences["recent_tool_names"],
            *seed["recent_tool_names"],
        ],
        limit=5,
    )
    seed["recent_tool_namespaces"] = compact_recent_values(
        [
            *cross_workspace_preferences["recent_tool_namespaces"],
            *seed["recent_tool_namespaces"],
        ],
        limit=5,
    )
    updated_payload = dict(context_payload)
    updated_payload["agent_memory"] = {
        **(
            dict(updated_payload.get("agent_memory"))
            if isinstance(updated_payload.get("agent_memory"), dict)
            else {}
        ),
        **seed,
        "updated_at": utc_now().isoformat(),
    }
    updated_payload["agent_recent_tool_names"] = seed["recent_tool_names"]
    updated_payload["agent_recent_tool_namespaces"] = seed["recent_tool_namespaces"]
    updated_payload["agent_recent_objectives"] = seed["recent_objectives"]
    updated_payload["agent_recent_entity_names"] = seed["recent_entity_names"]
    updated_payload["agent_recent_period_labels"] = seed["recent_period_labels"]
    updated_payload["agent_recent_target_labels"] = seed["recent_target_labels"]
    return updated_payload


def merge_context_payload_with_cross_thread_memory(
    *,
    context_payload: dict[str, Any],
    recent_context_payloads: tuple[dict[str, Any], ...],
    cross_workspace_recent_context_payloads: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Return one context payload with cross-thread operator memory merged in."""

    current_snapshot = extract_operator_memory_snapshot(context_payload=context_payload)
    carry_forward = build_cross_thread_memory_seed(recent_context_payloads=recent_context_payloads)
    cross_workspace_preferences = build_cross_workspace_preference_seed(
        recent_context_payloads=cross_workspace_recent_context_payloads
    )
    updated_payload = dict(context_payload)
    updated_payload["agent_memory"] = {
        **(
            dict(updated_payload.get("agent_memory"))
            if isinstance(updated_payload.get("agent_memory"), dict)
            else {}
        ),
        "preferred_explanation_depth": (
            current_snapshot["preferred_explanation_depth"]
            if current_snapshot["preferred_explanation_depth"]
            != DEFAULT_PREFERRED_EXPLANATION_DEPTH
            else (
                carry_forward["preferred_explanation_depth"]
                if carry_forward["preferred_explanation_depth"]
                != DEFAULT_PREFERRED_EXPLANATION_DEPTH
                else cross_workspace_preferences["preferred_explanation_depth"]
            )
        ),
        "preferred_confirmation_style": (
            current_snapshot["preferred_confirmation_style"]
            if current_snapshot["preferred_confirmation_style"]
            != DEFAULT_PREFERRED_CONFIRMATION_STYLE
            else (
                carry_forward["preferred_confirmation_style"]
                if carry_forward["preferred_confirmation_style"]
                != DEFAULT_PREFERRED_CONFIRMATION_STYLE
                else cross_workspace_preferences["preferred_confirmation_style"]
            )
        ),
        "recent_tool_names": compact_recent_values(
            [
                *cross_workspace_preferences["recent_tool_names"],
                *carry_forward["recent_tool_names"],
                *current_snapshot["recent_tool_names"],
            ],
            limit=5,
        ),
        "recent_tool_namespaces": compact_recent_values(
            [
                *cross_workspace_preferences["recent_tool_namespaces"],
                *carry_forward["recent_tool_namespaces"],
                *current_snapshot["recent_tool_namespaces"],
            ],
            limit=5,
        ),
        "recent_objectives": compact_recent_values(
            [*carry_forward["recent_objectives"], *current_snapshot["recent_objectives"]],
            limit=4,
        ),
        "recent_entity_names": compact_recent_values(
            [*carry_forward["recent_entity_names"], *current_snapshot["recent_entity_names"]],
            limit=4,
        ),
        "recent_period_labels": compact_recent_values(
            [*carry_forward["recent_period_labels"], *current_snapshot["recent_period_labels"]],
            limit=4,
        ),
        "recent_target_labels": compact_recent_values(
            [*carry_forward["recent_target_labels"], *current_snapshot["recent_target_labels"]],
            limit=5,
        ),
        "last_target_type": current_snapshot["last_target_type"],
        "last_target_id": current_snapshot["last_target_id"],
        "last_target_label": current_snapshot["last_target_label"],
        "working_subtask": (
            current_snapshot["working_subtask"]
            if current_snapshot["working_subtask"]
            else carry_forward["working_subtask"]
        ),
        "approved_objective": (
            current_snapshot["approved_objective"]
            if current_snapshot["approved_objective"]
            else carry_forward["approved_objective"]
        ),
        "pending_branch": (
            current_snapshot["pending_branch"]
            if current_snapshot["pending_branch"]
            else carry_forward["pending_branch"]
        ),
        "last_async_status": (
            current_snapshot["last_async_status"] or carry_forward["last_async_status"]
        ),
        "last_async_objective": (
            current_snapshot["last_async_objective"] or carry_forward["last_async_objective"]
        ),
        "last_async_note": current_snapshot["last_async_note"] or carry_forward["last_async_note"],
        "updated_at": utc_now().isoformat(),
    }
    updated_payload["agent_recent_tool_names"] = updated_payload["agent_memory"][
        "recent_tool_names"
    ]
    updated_payload["agent_recent_tool_namespaces"] = updated_payload["agent_memory"][
        "recent_tool_namespaces"
    ]
    updated_payload["agent_recent_objectives"] = updated_payload["agent_memory"][
        "recent_objectives"
    ]
    updated_payload["agent_recent_entity_names"] = updated_payload["agent_memory"][
        "recent_entity_names"
    ]
    updated_payload["agent_recent_period_labels"] = updated_payload["agent_memory"][
        "recent_period_labels"
    ]
    updated_payload["agent_recent_target_labels"] = updated_payload["agent_memory"][
        "recent_target_labels"
    ]
    return updated_payload


__all__ = [
    "DEFAULT_PREFERRED_CONFIRMATION_STYLE",
    "DEFAULT_PREFERRED_EXPLANATION_DEPTH",
    "build_cross_thread_memory_seed",
    "build_cross_workspace_preference_seed",
    "build_recovery_guidance",
    "compact_recent_values",
    "extract_operator_memory_snapshot",
    "merge_context_payload_with_cross_thread_memory",
    "optional_memory_int",
    "optional_memory_text",
    "seed_context_payload_with_operator_memory",
]
