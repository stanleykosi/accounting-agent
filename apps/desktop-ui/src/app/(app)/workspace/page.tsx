/*
Purpose: Render the first protected workspace page for authenticated desktop operators.
Scope: Authenticated dashboard content that introduces the workflow backbone and runtime boundaries.
Dependencies: Shared UI primitives, global workspace styling, and the canonical workflow vocabulary.
*/

import type { ReactElement } from "react";
import {
  SurfaceCard,
  WorkflowPhaseStrip,
  confidencePalette,
  workflowPhases,
} from "@accounting-ai-agent/ui";

const desktopPrinciples = [
  "Close-run centric workflow state across Collection, Processing, Reconciliation, Reporting, and Review / Sign-off.",
  "Fail-fast diagnostics over compatibility shims so accounting exceptions stay visible and reviewable.",
  "Standalone Next.js output ready for Tauri sidecar packaging without introducing a second client architecture.",
];

const runtimeSurfaces = [
  "FastAPI application server for typed state changes and orchestration boundaries.",
  "Celery workers for OCR, extraction, reconciliation, and report generation jobs.",
  "Shared UI package for dense, evidence-linked finance workflows with one visual language.",
];

/**
 * Purpose: Render the initial authenticated dashboard surface for local finance operators.
 * Inputs: None.
 * Outputs: A server-rendered workspace overview page inside the protected app shell.
 * Behavior: Reuses shared UI primitives so the protected route already matches the canonical design system.
 */
export default function WorkspacePage(): ReactElement {
  return (
    <div className="app-shell">
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Canonical Desktop Workspace</p>
          <h1>
            Enterprise close management with evidence, review controls, and local-first runtime
            boundaries.
          </h1>
          <p className="lede">
            This workspace keeps the UI aligned with the accounting workflow source of truth while
            staying ready for Tauri sidecar packaging and generated SDK consumption.
          </p>
        </div>

        <SurfaceCard
          title="Confidence Semantics"
          subtitle="Shared tokens for review surfaces"
          tone="accent"
        >
          <div className="confidence-list">
            {Object.entries(confidencePalette).map(([label, color]) => (
              <div className="confidence-row" key={label}>
                <span className="confidence-swatch" style={{ backgroundColor: color }} />
                <span>{label}</span>
              </div>
            ))}
          </div>
        </SurfaceCard>
      </section>

      <WorkflowPhaseStrip
        phases={workflowPhases.map((phase, index) => ({
          description:
            index === 0
              ? "Gather and validate source documents before downstream work begins."
              : index === workflowPhases.length - 1
                ? "Capture approvals, overrides, exports, and immutable sign-off history."
                : "Advance only when upstream controls and typed workflow checks are satisfied.",
          name: phase.label,
        }))}
      />

      <section className="content-grid">
        <SurfaceCard title="Workspace Principles" subtitle="What this app foundation preserves">
          <ul className="detail-list">
            {desktopPrinciples.map((principle) => (
              <li key={principle}>{principle}</li>
            ))}
          </ul>
        </SurfaceCard>

        <SurfaceCard title="Runtime Surfaces" subtitle="How the TypeScript layer fits the platform">
          <ul className="detail-list">
            {runtimeSurfaces.map((surface) => (
              <li key={surface}>{surface}</li>
            ))}
          </ul>
        </SurfaceCard>
      </section>
    </div>
  );
}
