/*
Purpose: Render a compact before-and-after diff block for reviewer decisions and proposed changes.
Scope: Shared line-level diff alignment, empty-state handling, and change-status signaling.
Dependencies: React plus shared review tokens from packages/ui/src/styles/tokens.ts.
*/

import type { CSSProperties, ReactElement } from "react";
import { reviewSpacing, reviewSurfacePalette } from "../../styles/tokens";

export type DiffViewerProps = {
  afterLabel?: string;
  afterValue: string | null | undefined;
  beforeLabel?: string;
  beforeValue: string | null | undefined;
  emptyValueLabel?: string;
  title?: string;
};

type DiffCellKind = "added" | "empty" | "equal" | "removed";

type DiffRow = Readonly<{
  after: {
    kind: DiffCellKind;
    value: string;
  };
  before: {
    kind: DiffCellKind;
    value: string;
  };
  id: string;
}>;

type DiffOperation = Readonly<{
  kind: "add" | "equal" | "remove";
  value: string;
}>;

const sectionStyle: CSSProperties = {
  border: `1px solid ${reviewSurfacePalette.border}`,
  borderRadius: "16px",
  display: "grid",
  gap: reviewSpacing.panel,
  padding: reviewSpacing.panel,
  background: reviewSurfacePalette.panelStrong,
};

/**
 * Purpose: Render a shared diff surface for review-state and change-comparison workflows.
 * Inputs: Before and after values plus optional labels and title copy.
 * Outputs: A React element with aligned before/after columns and line-level change emphasis.
 * Behavior: Falls back to a "No value" placeholder when one side of the diff is absent.
 */
export function DiffViewer({
  afterLabel = "After",
  afterValue,
  beforeLabel = "Before",
  beforeValue,
  emptyValueLabel = "No value",
  title = "Review diff",
}: Readonly<DiffViewerProps>): ReactElement {
  const rows = buildDiffRows(beforeValue, afterValue, emptyValueLabel);
  const hasChanges = rows.some((row) => row.before.kind !== "equal" || row.after.kind !== "equal");

  return (
    <section className="ui-diff-viewer" style={sectionStyle}>
      <header
        style={{
          alignItems: "center",
          display: "flex",
          flexWrap: "wrap",
          gap: reviewSpacing.dense,
        }}
      >
        <strong style={{ fontSize: "0.92rem", letterSpacing: "-0.02em" }}>{title}</strong>
        <span
          className={`ui-diff-status-chip ${hasChanges ? "changed" : "stable"}`}
          style={{
            alignItems: "center",
            background: hasChanges ? "rgba(231, 169, 59, 0.16)" : "rgba(31, 169, 113, 0.16)",
            borderRadius: "999px",
            color: hasChanges ? "#7a4b02" : "#0f6b45",
            display: "inline-flex",
            fontSize: "0.72rem",
            fontWeight: 700,
            minHeight: "24px",
            padding: "0 8px",
            textTransform: "uppercase",
          }}
        >
          {hasChanges ? "Pending changes" : "No change"}
        </span>
      </header>

      <div className="ui-diff-grid">
        <DiffColumn label={beforeLabel} rows={rows} side="before" />
        <DiffColumn label={afterLabel} rows={rows} side="after" />
      </div>
    </section>
  );
}

function DiffColumn({
  label,
  rows,
  side,
}: Readonly<{
  label: string;
  rows: readonly DiffRow[];
  side: "after" | "before";
}>): ReactElement {
  return (
    <div className="ui-diff-column">
      <p className="ui-diff-column-label">{label}</p>
      <ul className="ui-diff-line-list">
        {rows.map((row) => {
          const cell = row[side];
          return (
            <li className={`ui-diff-line ui-diff-line-${cell.kind}`} key={`${row.id}:${side}`}>
              {cell.value}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function buildDiffRows(
  beforeValue: string | null | undefined,
  afterValue: string | null | undefined,
  emptyValueLabel: string,
): readonly DiffRow[] {
  const beforeLines = normalizeDiffLines(beforeValue, emptyValueLabel);
  const afterLines = normalizeDiffLines(afterValue, emptyValueLabel);
  const operations = buildLineOperations(beforeLines, afterLines);

  return operations.map((operation, index) => {
    switch (operation.kind) {
      case "equal":
        return {
          after: { kind: "equal", value: operation.value },
          before: { kind: "equal", value: operation.value },
          id: `diff-row-${index}`,
        };
      case "remove":
        return {
          after: { kind: "empty", value: emptyValueLabel },
          before: { kind: "removed", value: operation.value },
          id: `diff-row-${index}`,
        };
      case "add":
        return {
          after: { kind: "added", value: operation.value },
          before: { kind: "empty", value: emptyValueLabel },
          id: `diff-row-${index}`,
        };
    }
  });
}

function normalizeDiffLines(value: string | null | undefined, emptyValueLabel: string): string[] {
  const normalized = value?.trim();
  if (!normalized) {
    return [emptyValueLabel];
  }

  return normalized.split("\n").map((line) => line.trimEnd());
}

function buildLineOperations(
  beforeLines: readonly string[],
  afterLines: readonly string[],
): readonly DiffOperation[] {
  const matrix = buildLcsMatrix(beforeLines, afterLines);
  const operations: DiffOperation[] = [];

  let beforeIndex = beforeLines.length;
  let afterIndex = afterLines.length;

  // Walk the matrix backwards so insertions and removals remain aligned for the final row view.
  while (beforeIndex > 0 || afterIndex > 0) {
    if (
      beforeIndex > 0 &&
      afterIndex > 0 &&
      beforeLines[beforeIndex - 1] === afterLines[afterIndex - 1]
    ) {
      operations.unshift({
        kind: "equal",
        value: beforeLines[beforeIndex - 1] ?? "",
      });
      beforeIndex -= 1;
      afterIndex -= 1;
      continue;
    }

    if (
      afterIndex > 0 &&
      (beforeIndex === 0 ||
        (matrix[beforeIndex]?.[afterIndex - 1] ?? 0) >=
          (matrix[beforeIndex - 1]?.[afterIndex] ?? 0))
    ) {
      operations.unshift({
        kind: "add",
        value: afterLines[afterIndex - 1] ?? "",
      });
      afterIndex -= 1;
      continue;
    }

    operations.unshift({
      kind: "remove",
      value: beforeLines[beforeIndex - 1] ?? "",
    });
    beforeIndex -= 1;
  }

  return operations;
}

function buildLcsMatrix(beforeLines: readonly string[], afterLines: readonly string[]): number[][] {
  const matrix = Array.from({ length: beforeLines.length + 1 }, () =>
    Array.from({ length: afterLines.length + 1 }, () => 0),
  );

  for (let beforeIndex = 1; beforeIndex <= beforeLines.length; beforeIndex += 1) {
    for (let afterIndex = 1; afterIndex <= afterLines.length; afterIndex += 1) {
      if (beforeLines[beforeIndex - 1] === afterLines[afterIndex - 1]) {
        matrix[beforeIndex]![afterIndex] = (matrix[beforeIndex - 1]?.[afterIndex - 1] ?? 0) + 1;
        continue;
      }

      matrix[beforeIndex]![afterIndex] = Math.max(
        matrix[beforeIndex - 1]?.[afterIndex] ?? 0,
        matrix[beforeIndex]?.[afterIndex - 1] ?? 0,
      );
    }
  }

  return matrix;
}
