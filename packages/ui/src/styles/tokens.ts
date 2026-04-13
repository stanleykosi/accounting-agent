/*
Purpose: Define the canonical review-surface tokens for shared UI components and desktop styling.
Scope: Workflow labels plus color, spacing, and typography tokens used by the shared review toolkit.
Dependencies: packages/ui/src/lib/domain.ts for canonical workflow phase labels.
*/

import type { WorkflowPhase } from "../lib/domain";
import { workflowPhaseDefinitions } from "../lib/domain";

export type ConfidenceTone = "high" | "low" | "medium" | "unknown";

export const workflowPhases = [
  ...workflowPhaseDefinitions.map(({ code, label }) => ({
    id: code,
    label,
  })),
] as const satisfies readonly Readonly<{
  id: WorkflowPhase;
  label: string;
}>[];

export const reviewTypography = {
  body: '"Sora", "Avenir Next", "Segoe UI", sans-serif',
  display: '"Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif',
  mono: '"IBM Plex Mono", "SFMono-Regular", "Consolas", monospace',
} as const;

export const reviewSpacing = {
  dense: "8px",
  panel: "18px",
  section: "24px",
  roomy: "32px",
} as const;

export const reviewSurfacePalette = {
  border: "rgba(52, 72, 63, 0.14)",
  mutedText: "#4d5b52",
  panel: "rgba(255, 251, 245, 0.92)",
  panelStrong: "rgba(255, 255, 255, 0.78)",
  shadow: "0 18px 48px rgba(22, 32, 25, 0.08)",
  text: "#162019",
} as const;

export const confidencePalette = {
  high: {
    background: "rgba(31, 169, 113, 0.18)",
    border: "rgba(15, 107, 69, 0.18)",
    foreground: "#0f6b45",
  },
  low: {
    background: "rgba(217, 83, 79, 0.2)",
    border: "rgba(145, 37, 32, 0.18)",
    foreground: "#912520",
  },
  medium: {
    background: "rgba(231, 169, 59, 0.2)",
    border: "rgba(122, 75, 2, 0.18)",
    foreground: "#7a4b02",
  },
  unknown: {
    background: "rgba(130, 130, 130, 0.16)",
    border: "rgba(77, 91, 82, 0.14)",
    foreground: "#4d5b52",
  },
} as const satisfies Record<
  ConfidenceTone,
  Readonly<{
    background: string;
    border: string;
    foreground: string;
  }>
>;

export const exceptionPalette = {
  blocking: {
    background: "rgba(217, 83, 79, 0.16)",
    border: "rgba(145, 37, 32, 0.18)",
    foreground: "#912520",
  },
  neutral: {
    background: "rgba(77, 91, 82, 0.1)",
    border: "rgba(77, 91, 82, 0.12)",
    foreground: "#4d5b52",
  },
  positive: {
    background: "rgba(31, 169, 113, 0.16)",
    border: "rgba(15, 107, 69, 0.18)",
    foreground: "#0f6b45",
  },
  warning: {
    background: "rgba(231, 169, 59, 0.16)",
    border: "rgba(122, 75, 2, 0.18)",
    foreground: "#7a4b02",
  },
} as const;
