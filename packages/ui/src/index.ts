/*
Purpose: Expose the public API for the shared UI package.
Scope: Re-export reusable components and shared design/workflow tokens for frontend consumers.
Dependencies: The component and token modules within packages/ui/src.
*/

export { SurfaceCard } from "./components/SurfaceCard";
export type { SurfaceCardProps, SurfaceTone } from "./components/SurfaceCard";
export { WorkflowPhaseStrip } from "./components/WorkflowPhaseStrip";
export type { WorkflowPhaseItem, WorkflowPhaseStripProps } from "./components/WorkflowPhaseStrip";
export { confidencePalette, workflowPhases } from "./tokens";
