/*
Purpose: Expose the public API for the shared UI package.
Scope: Re-export reusable components and shared design/workflow tokens for frontend consumers.
Dependencies: The component, token, and domain modules within packages/ui/src.
*/

export { SurfaceCard } from "./components/SurfaceCard";
export type { SurfaceCardProps, SurfaceTone } from "./components/SurfaceCard";
export { AuthGate } from "./components/auth/AuthGate";
export type { AuthGateProps, AuthGateTone } from "./components/auth/AuthGate";
export { Timeline } from "./components/activity/Timeline";
export type { TimelineItem, TimelineItemTone, TimelineProps } from "./components/activity/Timeline";
export { PhaseProgress } from "./components/close-runs/PhaseProgress";
export type { PhaseProgressItem, PhaseProgressProps } from "./components/close-runs/PhaseProgress";
export { EvidenceDrawer } from "./components/evidence/EvidenceDrawer";
export type {
  EvidenceDrawerProps,
  EvidenceDrawerReference,
} from "./components/evidence/EvidenceDrawer";
export { AppShell } from "./components/layout/AppShell";
export type { AppShellNavigationItem, AppShellProps } from "./components/layout/AppShell";
export { CommandPalette } from "./components/layout/CommandPalette";
export type { CommandPaletteItem, CommandPaletteProps } from "./components/layout/CommandPalette";
export { OwnershipBadge } from "./components/ownership/OwnershipBadge";
export type {
  OwnershipBadgeOperator,
  OwnershipBadgeProps,
} from "./components/ownership/OwnershipBadge";
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
