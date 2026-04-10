/*
Purpose: Render the canonical five-phase workflow vocabulary as a shared UI component.
Scope: Lightweight workflow strip for dashboards, headers, and onboarding surfaces.
Dependencies: React and the workflow phase labels defined in packages/ui/src/tokens.ts.
*/

import type { CSSProperties, ReactElement } from "react";

export type WorkflowPhaseItem = {
  description: string;
  name: string;
};

export type WorkflowPhaseStripProps = {
  phases: readonly WorkflowPhaseItem[];
};

const containerStyle: CSSProperties = {
  display: "grid",
  gap: "16px",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  margin: "28px 0 24px",
};

/**
 * Purpose: Present workflow phases in the exact order required by the accounting specification.
 * Inputs: Ordered workflow phase metadata with a display name and supporting description.
 * Outputs: A responsive grid of phase cards suitable for server-rendered or client-rendered pages.
 * Behavior: Uses an explicit ordinal label to reinforce the non-negotiable workflow sequence.
 */
export function WorkflowPhaseStrip({ phases }: Readonly<WorkflowPhaseStripProps>): ReactElement {
  return (
    <section style={containerStyle}>
      {phases.map((phase, index) => (
        <article
          key={phase.name}
          style={{
            backdropFilter: "blur(14px)",
            background: "rgba(255, 251, 245, 0.8)",
            border: "1px solid rgba(52, 72, 63, 0.12)",
            borderRadius: "22px",
            display: "grid",
            gap: "10px",
            minHeight: "168px",
            padding: "20px",
          }}
        >
          <span
            style={{
              color: "#0f766e",
              fontSize: "0.76rem",
              fontWeight: 700,
              letterSpacing: "0.16em",
              textTransform: "uppercase",
            }}
          >
            Phase {index + 1}
          </span>
          <h2
            style={{
              fontSize: "1.22rem",
              letterSpacing: "-0.04em",
              lineHeight: 1.15,
              margin: 0,
            }}
          >
            {phase.name}
          </h2>
          <p
            style={{
              color: "#4d5b52",
              lineHeight: 1.55,
              margin: 0,
            }}
          >
            {phase.description}
          </p>
        </article>
      ))}
    </section>
  );
}
