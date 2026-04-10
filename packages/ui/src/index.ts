/*
Purpose: Expose the public API for the shared UI package.
Scope: Re-export reusable components and shared design/workflow tokens for frontend consumers.
Dependencies: The component, token, and domain modules within packages/ui/src.
*/

export { SurfaceCard } from "./components/SurfaceCard";
export type { SurfaceCardProps, SurfaceTone } from "./components/SurfaceCard";
export { WorkflowPhaseStrip } from "./components/WorkflowPhaseStrip";
export type { WorkflowPhaseItem, WorkflowPhaseStripProps } from "./components/WorkflowPhaseStrip";
export {
  artifactTypeDefinitions,
  artifactTypeOrder,
  autonomyModeDefinitions,
  autonomyModeOrder,
  closeRunPhaseStatusDefinitions,
  closeRunPhaseStatusOrder,
  closeRunStatusDefinitions,
  closeRunStatusOrder,
  getWorkflowPhaseDefinition,
  getWorkflowPhaseItems,
  jobStatusDefinitions,
  jobStatusOrder,
  reviewStatusDefinitions,
  reviewStatusOrder,
  workflowPhaseDefinitions,
  workflowPhaseOrder,
} from "./lib/domain";
export type {
  ArtifactType,
  AutonomyMode,
  CloseRunPhaseStatus,
  CloseRunStatus,
  JobStatus,
  ReviewStatus,
  WorkflowPhase,
  WorkflowPhaseDefinition,
} from "./lib/domain";
export { confidencePalette, workflowPhases } from "./tokens";
