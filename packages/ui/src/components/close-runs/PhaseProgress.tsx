/*
Purpose: Render the canonical close-run phase progression with status and blocker context.
Scope: Phase ordering, status display, active-phase emphasis, and compact explanatory metadata.
Dependencies: Shared domain metadata in packages/ui/src/lib/domain.ts and React rendering.
*/

import type { CSSProperties, ReactElement } from "react";
import {
  closeRunPhaseStatusDefinitions,
  getWorkflowPhaseDefinition,
  type CloseRunPhaseStatus,
  type WorkflowPhase,
} from "../../lib/domain";

export type PhaseProgressItem = Readonly<{
  blockingReason?: string | null;
  completedAt?: string | null;
  isCurrent?: boolean;
  phase: WorkflowPhase;
  status: CloseRunPhaseStatus;
}>;

export type PhaseProgressProps = Readonly<{
  items: readonly PhaseProgressItem[];
}>;

const trackStyle: CSSProperties = {
  display: "grid",
  gap: "14px",
  listStyle: "none",
  margin: 0,
  padding: 0,
};

const statusPalette: Readonly<
  Record<CloseRunPhaseStatus, Readonly<{ background: string; color: string }>>
> = {
  blocked: {
    background: "rgba(185, 28, 28, 0.12)",
    color: "#8f1d1d",
  },
  completed: {
    background: "rgba(15, 118, 110, 0.14)",
    color: "#0f6b45",
  },
  in_progress: {
    background: "rgba(217, 119, 6, 0.14)",
    color: "#8d4a02",
  },
  not_started: {
    background: "rgba(77, 91, 82, 0.12)",
    color: "#4d5b52",
  },
  ready: {
    background: "rgba(20, 108, 99, 0.14)",
    color: "#114f49",
  },
};

/**
 * Purpose: Render a five-step workflow track for one close run.
 * Inputs: Ordered phase items with workflow codes, statuses, active markers, and optional blocker details.
 * Outputs: A React element suitable for dashboard, workspace, and close-run detail surfaces.
 * Behavior: Keeps the vocabulary anchored to the shared domain catalog so UI labels cannot drift.
 */
export function PhaseProgress({ items }: PhaseProgressProps): ReactElement {
  return (
    <ol style={trackStyle}>
      {items.map((item, index) => {
        const phase = getWorkflowPhaseDefinition(item.phase);
        const statusDefinition = closeRunPhaseStatusDefinitions.find(
          (definition) => definition.code === item.status,
        );
        const palette = statusPalette[item.status];
        const detail =
          item.blockingReason?.trim() ||
          (item.status === "completed" && item.completedAt
            ? `Completed ${formatTimestamp(item.completedAt)}`
            : phase.description);

        return (
          <li
            key={item.phase}
            style={{
              border: item.isCurrent
                ? "1px solid rgba(15, 118, 110, 0.26)"
                : "1px solid rgba(52, 72, 63, 0.12)",
              borderRadius: "18px",
              display: "grid",
              gap: "10px",
              padding: "16px 18px",
              position: "relative",
              background: item.isCurrent
                ? "rgba(214, 230, 221, 0.42)"
                : "rgba(255, 255, 255, 0.56)",
            }}
          >
            {index < items.length - 1 ? (
              <span
                aria-hidden="true"
                style={{
                  background: "rgba(52, 72, 63, 0.12)",
                  bottom: "-14px",
                  left: "30px",
                  position: "absolute",
                  top: "100%",
                  width: "2px",
                }}
              />
            ) : null}

            <div
              style={{
                alignItems: "center",
                display: "flex",
                gap: "12px",
                justifyContent: "space-between",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                <span
                  style={{
                    alignItems: "center",
                    background: "rgba(255, 251, 245, 0.94)",
                    border: "1px solid rgba(52, 72, 63, 0.12)",
                    borderRadius: "999px",
                    display: "inline-flex",
                    fontSize: "0.78rem",
                    fontWeight: 700,
                    height: "32px",
                    justifyContent: "center",
                    minWidth: "32px",
                  }}
                >
                  {phase.ordinal}
                </span>
                <div style={{ display: "grid", gap: "4px" }}>
                  <strong style={{ fontSize: "1rem", letterSpacing: "-0.03em" }}>
                    {phase.label}
                  </strong>
                  <span
                    style={{
                      color: "#4d5b52",
                      fontSize: "0.82rem",
                      fontWeight: 700,
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                    }}
                  >
                    {item.isCurrent ? "Active phase" : "Workflow phase"}
                  </span>
                </div>
              </div>

              <span
                style={{
                  alignItems: "center",
                  background: palette.background,
                  borderRadius: "999px",
                  color: palette.color,
                  display: "inline-flex",
                  fontSize: "0.8rem",
                  fontWeight: 700,
                  minHeight: "28px",
                  padding: "0 10px",
                  textTransform: "capitalize",
                }}
              >
                {statusDefinition?.label ?? item.status.replaceAll("_", " ")}
              </span>
            </div>

            <p style={{ color: "#4d5b52", lineHeight: 1.65, margin: 0 }}>{detail}</p>
          </li>
        );
      })}
    </ol>
  );
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
