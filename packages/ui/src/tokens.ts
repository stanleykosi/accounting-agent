/*
Purpose: Define the first shared design tokens and workflow labels for desktop surfaces.
Scope: Visual constants and canonical workflow labels consumed by the shared UI package and the desktop app.
Dependencies: packages/ui/src/lib/domain.ts plus TypeScript and React consumers that import
these values.
*/

import type { WorkflowPhase } from "./lib/domain";
import { workflowPhaseDefinitions } from "./lib/domain";

export const workflowPhases = [
  ...workflowPhaseDefinitions.map(({ code, label }) => ({
    id: code,
    label,
  })),
] as const satisfies readonly Readonly<{
  id: WorkflowPhase;
  label: string;
}>[];

export const confidencePalette = {
  High: "#0f766e",
  Medium: "#d97706",
  Low: "#b91c1c",
} as const;
