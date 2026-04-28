"""
Purpose: Provide reusable Rich table builders for keyboard-first CLI workflows.
Scope: Convert API response rows into dense status tables with canonical badges,
safe value formatting, and predictable empty-state rendering.
Dependencies: Rich Table/Text classes and the CLI domain badge helpers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from apps.cli.src.domain import get_cli_badge
from rich.table import Table
from rich.text import Text
from services.common.enums import (
    ArtifactType,
    AutonomyMode,
    CloseRunStatus,
    JobStatus,
    ReviewStatus,
    WorkflowPhase,
)

type OverflowMethod = Literal["fold", "crop", "ellipsis", "ignore"]
type BadgeEnum = (
    WorkflowPhase | CloseRunStatus | JobStatus | ReviewStatus | AutonomyMode | ArtifactType
)


@dataclass(frozen=True, slots=True)
class StatusColumn:
    """Describe one column projected from an API row into a Rich status table."""

    header: str
    key: str
    style: str = ""
    max_width: int | None = None
    overflow: OverflowMethod = "fold"
    badge: bool = False


def build_status_table(
    *,
    title: str,
    columns: Sequence[StatusColumn],
    rows: Iterable[Mapping[str, object]],
    caption: str | None = None,
) -> Table:
    """Build a dense Rich table for CLI dashboards and command responses."""

    table = Table(title=title, caption=caption, expand=True, show_lines=False)
    for column in columns:
        table.add_column(
            column.header,
            style=column.style,
            max_width=column.max_width,
            overflow=column.overflow,
        )

    row_count = 0
    for row in rows:
        table.add_row(
            *(_render_cell(row.get(column.key), badge=column.badge) for column in columns)
        )
        row_count += 1

    if row_count == 0:
        table.add_row(*(_empty_cell(index) for index, _ in enumerate(columns)))

    return table


def _render_cell(value: object, *, badge: bool) -> Text:
    """Render one table cell, applying canonical domain badge styling when possible."""

    if badge:
        domain_value = _coerce_badge_value(value)
        if domain_value is not None:
            badge_value = get_cli_badge(domain_value)
            return Text(badge_value.text, style=badge_value.style)

    if value is None:
        return Text("—", style="dim")

    if isinstance(value, bool):
        return Text("yes" if value else "no", style="green" if value else "yellow")

    return Text(str(value))


def _empty_cell(index: int) -> Text:
    """Render an empty-state row while preserving the table's column count."""

    if index == 0:
        return Text("No records returned.", style="dim")

    return Text("")


def _coerce_badge_value(value: object) -> BadgeEnum | None:
    """Convert common API enum strings into canonical enum objects for badge rendering."""

    if isinstance(value, (WorkflowPhase, CloseRunStatus, JobStatus, ReviewStatus, AutonomyMode)):
        return value

    if not isinstance(value, str):
        return None

    for enum_member in (
        *tuple(WorkflowPhase),
        *tuple(CloseRunStatus),
        *tuple(JobStatus),
        *tuple(ReviewStatus),
        *tuple(AutonomyMode),
        *tuple(ArtifactType),
    ):
        if enum_member.value == value:
            return cast(BadgeEnum, enum_member)

    return None


__all__ = ["StatusColumn", "build_status_table"]
