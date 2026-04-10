/*
Purpose: Define the first shared design tokens and workflow labels for desktop surfaces.
Scope: Visual constants and canonical workflow labels consumed by the shared UI package and the desktop app.
Dependencies: None at runtime beyond TypeScript and React consumers that import these values.
*/

export const workflowPhases = [
  {
    id: "collection",
    label: "Collection",
  },
  {
    id: "processing",
    label: "Processing",
  },
  {
    id: "reconciliation",
    label: "Reconciliation",
  },
  {
    id: "reporting",
    label: "Reporting",
  },
  {
    id: "review_signoff",
    label: "Review / Sign-off",
  },
] as const;

export const confidencePalette = {
  High: "#0f766e",
  Medium: "#d97706",
  Low: "#b91c1c",
} as const;
