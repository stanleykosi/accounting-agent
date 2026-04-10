/*
Purpose: Mirror the canonical workflow and lifecycle language for UI rendering.
Scope: Shared enum-like string unions, ordered definitions, and label helpers used
by desktop components without waiting on generated SDK consumers.
Dependencies: TypeScript only, so the shared UI package can expose zero-runtime
domain metadata to every frontend surface.
*/

type DomainDefinition<TCode extends string> = Readonly<{
  code: TCode;
  description: string;
  label: string;
}>;

type WorkflowPhaseMetadata = Readonly<{
  description: string;
  label: string;
}>;

export type WorkflowPhase =
  | "collection"
  | "processing"
  | "reconciliation"
  | "reporting"
  | "review_signoff";

export type CloseRunStatus =
  | "draft"
  | "in_review"
  | "approved"
  | "exported"
  | "archived"
  | "reopened";

export type CloseRunPhaseStatus =
  | "not_started"
  | "in_progress"
  | "blocked"
  | "ready"
  | "completed";

export type JobStatus = "queued" | "running" | "blocked" | "failed" | "canceled" | "completed";

export type AutonomyMode = "human_review" | "reduced_interruption";

export type ReviewStatus =
  | "draft"
  | "pending_review"
  | "approved"
  | "rejected"
  | "superseded"
  | "applied";

export type ArtifactType =
  | "report_excel"
  | "report_pdf"
  | "audit_trail"
  | "evidence_pack"
  | "quickbooks_export";

export type WorkflowPhaseDefinition = DomainDefinition<WorkflowPhase> &
  Readonly<{
    ordinal: number;
  }>;

const workflowPhaseMetadata = {
  collection: {
    description: "Collect required source documents and validate that the close run can proceed.",
    label: "Collection",
  },
  processing: {
    description:
      "Parse documents, extract fields, and draft accounting recommendations with evidence.",
    label: "Processing",
  },
  reconciliation: {
    description:
      "Resolve matches, exceptions, and control checks before reports are prepared.",
    label: "Reconciliation",
  },
  reporting: {
    description:
      "Generate the required statements, schedules, commentary, and export-ready outputs.",
    label: "Reporting",
  },
  review_signoff: {
    description:
      "Capture reviewer decisions, sign-off records, and release controls for the period.",
    label: "Review / Sign-off",
  },
} as const satisfies Record<WorkflowPhase, WorkflowPhaseMetadata>;

const closeRunStatusMetadata = {
  draft: {
    description: "The close run is being assembled and has not entered formal review yet.",
    label: "Draft",
  },
  in_review: {
    description: "The close run is active and waiting on reviewer actions or unresolved issues.",
    label: "In review",
  },
  approved: {
    description: "All required review decisions were recorded and the close run was signed off.",
    label: "Approved",
  },
  exported: {
    description:
      "Release artifacts or export-ready files were issued for this close run version.",
    label: "Exported",
  },
  archived: {
    description:
      "The close run is closed to normal editing and retained for traceable history.",
    label: "Archived",
  },
  reopened: {
    description: "A previously approved or exported period was reopened as a new working state.",
    label: "Reopened",
  },
} as const satisfies Record<CloseRunStatus, WorkflowPhaseMetadata>;

const closeRunPhaseStatusMetadata = {
  not_started: {
    description: "Work for this phase has not begun yet.",
    label: "Not started",
  },
  in_progress: {
    description: "This phase has active work underway but is not ready to advance.",
    label: "In progress",
  },
  blocked: {
    description: "This phase cannot advance until an explicit blocking issue is resolved.",
    label: "Blocked",
  },
  ready: {
    description: "This phase passed its entry checks and is ready for human or system execution.",
    label: "Ready",
  },
  completed: {
    description: "This phase finished and the close run can move to the next gate.",
    label: "Completed",
  },
} as const satisfies Record<CloseRunPhaseStatus, WorkflowPhaseMetadata>;

const jobStatusMetadata = {
  queued: {
    description: "The job was accepted and is waiting for worker capacity.",
    label: "Queued",
  },
  running: {
    description: "The worker is actively executing the job.",
    label: "Running",
  },
  blocked: {
    description: "The job is paused on a dependency, input issue, or manual recovery step.",
    label: "Blocked",
  },
  failed: {
    description:
      "The job stopped with an error and requires explicit retry or intervention.",
    label: "Failed",
  },
  canceled: {
    description: "Execution was intentionally stopped before normal completion.",
    label: "Canceled",
  },
  completed: {
    description: "The job finished successfully and its outputs are ready for use.",
    label: "Completed",
  },
} as const satisfies Record<JobStatus, WorkflowPhaseMetadata>;

const autonomyModeMetadata = {
  human_review: {
    description:
      "Suggested changes must wait for explicit human approval before they apply.",
    label: "Human review",
  },
  reduced_interruption: {
    description:
      "Low-risk changes may update working state after policy checks while staying audited.",
    label: "Reduced interruption",
  },
} as const satisfies Record<AutonomyMode, WorkflowPhaseMetadata>;

const reviewStatusMetadata = {
  draft: {
    description: "The item exists as a working proposal that has not entered review routing yet.",
    label: "Draft",
  },
  pending_review: {
    description:
      "The item is waiting for a reviewer because autonomy or policy prevented direct apply.",
    label: "Pending review",
  },
  approved: {
    description:
      "A reviewer accepted the item and it is eligible for downstream materialization.",
    label: "Approved",
  },
  rejected: {
    description: "A reviewer declined the item and it should not affect current working state.",
    label: "Rejected",
  },
  superseded: {
    description: "A newer revision replaced this item before it reached a terminal outcome.",
    label: "Superseded",
  },
  applied: {
    description:
      "The reviewed item was committed into working accounting state with lineage preserved.",
    label: "Applied",
  },
} as const satisfies Record<ReviewStatus, WorkflowPhaseMetadata>;

const artifactTypeMetadata = {
  report_excel: {
    description: "Accountant-ready Excel workbook pack generated for a close run version.",
    label: "Excel report pack",
  },
  report_pdf: {
    description: "Executive-ready PDF management report pack generated for a close run version.",
    label: "PDF report pack",
  },
  audit_trail: {
    description: "Immutable approval, override, and change-history export for a close run.",
    label: "Audit trail export",
  },
  evidence_pack: {
    description: "Bundle of source references, extracted values, approvals, diffs, and outputs.",
    label: "Evidence pack",
  },
  quickbooks_export: {
    description:
      "Stable export-ready file prepared for accountant upload into QuickBooks Online.",
    label: "QuickBooks export file",
  },
} as const satisfies Record<ArtifactType, WorkflowPhaseMetadata>;

function buildDomainDefinitions<TCode extends string>(
  orderedCodes: readonly TCode[],
  metadata: Readonly<Record<TCode, WorkflowPhaseMetadata>>,
): readonly DomainDefinition<TCode>[] {
  return orderedCodes.map((code) => ({
    code,
    description: metadata[code].description,
    label: metadata[code].label,
  }));
}

export const workflowPhaseOrder = [
  "collection",
  "processing",
  "reconciliation",
  "reporting",
  "review_signoff",
] as const satisfies readonly WorkflowPhase[];

export const closeRunStatusOrder = [
  "draft",
  "in_review",
  "approved",
  "exported",
  "archived",
  "reopened",
] as const satisfies readonly CloseRunStatus[];

export const closeRunPhaseStatusOrder = [
  "not_started",
  "in_progress",
  "blocked",
  "ready",
  "completed",
] as const satisfies readonly CloseRunPhaseStatus[];

export const jobStatusOrder = [
  "queued",
  "running",
  "blocked",
  "failed",
  "canceled",
  "completed",
] as const satisfies readonly JobStatus[];

export const autonomyModeOrder = [
  "human_review",
  "reduced_interruption",
] as const satisfies readonly AutonomyMode[];

export const reviewStatusOrder = [
  "draft",
  "pending_review",
  "approved",
  "rejected",
  "superseded",
  "applied",
] as const satisfies readonly ReviewStatus[];

export const artifactTypeOrder = [
  "report_excel",
  "report_pdf",
  "audit_trail",
  "evidence_pack",
  "quickbooks_export",
] as const satisfies readonly ArtifactType[];

export const workflowPhaseDefinitions: readonly WorkflowPhaseDefinition[] = workflowPhaseOrder.map(
  (code, index) => ({
    code,
    description: workflowPhaseMetadata[code].description,
    label: workflowPhaseMetadata[code].label,
    ordinal: index + 1,
  }),
);

export const closeRunStatusDefinitions = buildDomainDefinitions(
  closeRunStatusOrder,
  closeRunStatusMetadata,
);

export const closeRunPhaseStatusDefinitions = buildDomainDefinitions(
  closeRunPhaseStatusOrder,
  closeRunPhaseStatusMetadata,
);

export const jobStatusDefinitions = buildDomainDefinitions(jobStatusOrder, jobStatusMetadata);

export const autonomyModeDefinitions = buildDomainDefinitions(
  autonomyModeOrder,
  autonomyModeMetadata,
);

export const reviewStatusDefinitions = buildDomainDefinitions(
  reviewStatusOrder,
  reviewStatusMetadata,
);

export const artifactTypeDefinitions = buildDomainDefinitions(
  artifactTypeOrder,
  artifactTypeMetadata,
);

/**
 * Purpose: Resolve the display metadata for one workflow phase without repeating string literals.
 * Inputs: A canonical workflow phase code.
 * Outputs: The matching phase definition with ordinal, label, and description.
 * Behavior: Reads from the shared ordered definition list so UI surfaces stay aligned.
 */
export function getWorkflowPhaseDefinition(phase: WorkflowPhase): WorkflowPhaseDefinition {
  const definition = workflowPhaseDefinitions.find((candidate) => candidate.code === phase);
  if (definition === undefined) {
    throw new Error(`Unknown workflow phase: ${phase}`);
  }

  return definition;
}

/**
 * Purpose: Build the shared item shape consumed by the workflow strip component.
 * Inputs: None.
 * Outputs: Ordered workflow items with display names and descriptions.
 * Behavior: Preserves the exact backbone order required by the specification.
 */
export function getWorkflowPhaseItems(): readonly Readonly<{
  description: string;
  name: string;
}>[] {
  return workflowPhaseDefinitions.map((definition) => ({
    description: definition.description,
    name: definition.label,
  }));
}
