/*
Purpose: Render the first desktop UI route for the local demo workspace.
Scope: Server-rendered landing experience that introduces the workflow backbone and runtime surfaces.
Dependencies: Shared primitives from packages/ui and the global app stylesheet.
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
 * Purpose: Render the initial desktop dashboard surface for local demo operators.
 * Inputs: None.
 * Outputs: A server-rendered landing page that reflects the canonical architecture and workflow vocabulary.
 * Behavior: Uses shared UI components so the app and design package stay aligned from the first step.
 */
export default function HomePage(): ReactElement {
  return (
    <main className="app-shell">
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Canonical Desktop Workspace</p>
          <h1>
            Enterprise close management with evidence, review controls, and local-first runtime
            boundaries.
          </h1>
          <p className="lede">
            This frontend foundation keeps the UI aligned with the accounting workflow source of
            truth while staying ready for Tauri sidecar packaging and generated SDK consumption.
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
    </main>
  );
}
