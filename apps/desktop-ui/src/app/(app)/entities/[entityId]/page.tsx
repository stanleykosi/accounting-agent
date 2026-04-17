/*
Purpose: Render one authenticated entity workspace overview with settings, memberships, and activity history.
Scope: Client-side workspace reads plus update and membership-management flows against the same-origin entity proxy.
Dependencies: React hooks, Next.js route params, the entity API helpers, and shared UI surface cards.
*/

"use client";

import { SurfaceCard, Timeline, type TimelineItem } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AgentCapabilityCatalog } from "../../../../components/chat/AgentCapabilityCatalog";
import {
  use,
  useEffect,
  useMemo,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import {
  CloseRunApiError,
  createCloseRun,
  deriveCloseRunAttention,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  listCloseRuns,
  type CloseRunSummary,
} from "../../../../lib/close-runs";
import {
  EntityApiError,
  createEntityMembership,
  deleteEntityWorkspace,
  readEntityWorkspace,
  updateEntity,
  updateEntityMembership,
  type EntityWorkspace,
} from "../../../../lib/entities/api";

type EntityWorkspacePageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

type EntitySettingsFormState = {
  accountingStandard: string;
  autonomyMode: EntityWorkspace["autonomy_mode"];
  baseCurrency: string;
  countryCode: string;
  legalName: string;
  name: string;
  timezone: string;
};

type AddMembershipFormState = {
  isDefaultActor: boolean;
  role: string;
  userEmail: string;
};

type CreateCloseRunFormState = {
  periodEnd: string;
  periodStart: string;
  reportingCurrency: string;
};

type EntityActivityEvent = EntityWorkspace["activity_events"][number];

const defaultMembershipFormState: AddMembershipFormState = {
  isDefaultActor: false,
  role: "member",
  userEmail: "",
};

const defaultCreateCloseRunFormState: CreateCloseRunFormState = {
  periodEnd: "",
  periodStart: "",
  reportingCurrency: "NGN",
};

/**
 * Purpose: Render the overview screen for one entity workspace selected from the directory.
 * Inputs: The dynamic route params that identify which workspace should be loaded.
 * Outputs: A client-rendered workspace view with editable settings, memberships, and activity.
 * Behavior: Reads and mutates through the same-origin proxy so rotated auth cookies reach the browser.
 */
export default function EntityWorkspacePage({
  params,
}: Readonly<EntityWorkspacePageProps>): ReactElement {
  const { entityId } = use(params);
  const router = useRouter();
  const [entity, setEntity] = useState<EntityWorkspace | null>(null);
  const [entityErrorMessage, setEntityErrorMessage] = useState<string | null>(null);
  const [closeRunErrorMessage, setCloseRunErrorMessage] = useState<string | null>(null);
  const [closeRuns, setCloseRuns] = useState<readonly CloseRunSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();
  const [membershipFormState, setMembershipFormState] = useState<AddMembershipFormState>(
    defaultMembershipFormState,
  );
  const [closeRunFormState, setCloseRunFormState] = useState<CreateCloseRunFormState>(
    defaultCreateCloseRunFormState,
  );
  const [settingsFormState, setSettingsFormState] = useState<EntitySettingsFormState | null>(null);

  useEffect(() => {
    void loadWorkspaceView({
      entityId,
      onCloseRunsLoaded: setCloseRuns,
      onCloseRunError: setCloseRunErrorMessage,
      onError: setEntityErrorMessage,
      onLoaded: (workspace) => {
        setEntity(workspace);
        setSettingsFormState(deriveSettingsFormState(workspace));
      },
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

  const activityTimelineItems = useMemo<readonly TimelineItem[]>(
    () =>
      entity?.activity_events.map((activityEvent: EntityActivityEvent) => ({
        badge: activityEvent.actor?.full_name ?? "System",
        detail: `${activityEvent.summary} via ${activityEvent.source_surface}${
          activityEvent.trace_id ? ` • trace ${activityEvent.trace_id}` : ""
        }`,
        id: activityEvent.id,
        timestamp: formatDateTime(activityEvent.created_at),
        title: activityEvent.summary,
        tone: "default",
      })) ?? [],
    [entity],
  );

  const handleSettingsFieldChange =
    (fieldName: keyof EntitySettingsFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      setSettingsFormState((currentState) =>
        currentState === null
          ? currentState
          : {
              ...currentState,
              [fieldName]: event.target.value,
            },
      );
    };

  const handleMembershipFieldChange =
    (fieldName: keyof AddMembershipFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      const nextValue = fieldName === "isDefaultActor" ? event.target.checked : event.target.value;
      setMembershipFormState((currentState) => ({
        ...currentState,
        [fieldName]: nextValue,
      }));
    };

  const handleCloseRunFieldChange =
    (fieldName: keyof CreateCloseRunFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      setCloseRunFormState((currentState) => ({
        ...currentState,
        [fieldName]: event.target.value,
      }));
    };

  const handleEntityUpdate = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (settingsFormState === null) {
      return;
    }

    setEntityErrorMessage(null);
    startTransition(() => {
      void updateEntity(entityId, {
        accounting_standard: emptyStringToNull(settingsFormState.accountingStandard),
        autonomy_mode: settingsFormState.autonomyMode,
        base_currency: settingsFormState.baseCurrency,
        country_code: settingsFormState.countryCode,
        legal_name: emptyStringToNull(settingsFormState.legalName),
        name: settingsFormState.name,
        timezone: settingsFormState.timezone,
      })
        .then((workspace) => {
          setEntity(workspace);
          setSettingsFormState(deriveSettingsFormState(workspace));
          router.refresh();
        })
        .catch((error: unknown) => {
          setEntityErrorMessage(resolveEntityErrorMessage(error));
        });
    });
  };

  const handleAddMembership = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setEntityErrorMessage(null);

    startTransition(() => {
      void createEntityMembership(entityId, {
        is_default_actor: membershipFormState.isDefaultActor,
        role: membershipFormState.role,
        user_email: membershipFormState.userEmail,
      })
        .then((workspace) => {
          setEntity(workspace);
          setSettingsFormState(deriveSettingsFormState(workspace));
          setMembershipFormState(defaultMembershipFormState);
          router.refresh();
        })
        .catch((error: unknown) => {
          setEntityErrorMessage(resolveEntityErrorMessage(error));
        });
    });
  };

  const handleMembershipUpdate = (
    membershipId: string,
    payload: {
      is_default_actor?: boolean;
      role?: string;
    },
  ): void => {
    setEntityErrorMessage(null);

    startTransition(() => {
      void updateEntityMembership(entityId, membershipId, payload)
        .then((workspace) => {
          setEntity(workspace);
          setSettingsFormState(deriveSettingsFormState(workspace));
          router.refresh();
        })
        .catch((error: unknown) => {
          setEntityErrorMessage(resolveEntityErrorMessage(error));
        });
    });
  };

  const handleCreateCloseRun = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setCloseRunErrorMessage(null);
    const baseCurrency = entity?.base_currency ?? defaultCreateCloseRunFormState.reportingCurrency;
    const nextEntityId = entity?.id ?? entityId;

    startTransition(() => {
      void createCloseRun(entityId, {
        period_start: closeRunFormState.periodStart,
        period_end: closeRunFormState.periodEnd,
        reporting_currency: emptyStringToNull(closeRunFormState.reportingCurrency),
      })
        .then((createdCloseRun) => {
          setCloseRuns((currentCloseRuns) => [createdCloseRun, ...currentCloseRuns]);
          setCloseRunFormState({
            ...defaultCreateCloseRunFormState,
            reportingCurrency: baseCurrency,
          });
          router.push(`/entities/${nextEntityId}/close-runs/${createdCloseRun.id}`);
          router.refresh();
        })
        .catch((error: unknown) => {
          setCloseRunErrorMessage(resolveWorkspaceViewErrorMessage(error));
        });
    });
  };

  const handleDeleteWorkspace = (): void => {
    if (entity === null) {
      return;
    }
    const confirmed = window.confirm(
      `Delete ${entity.name}? This permanently removes the workspace, its close runs, documents, chat threads, and generated outputs.`,
    );
    if (!confirmed) {
      return;
    }

    setEntityErrorMessage(null);
    startTransition(() => {
      void deleteEntityWorkspace(entity.id)
        .then(() => {
          router.push("/entities");
          router.refresh();
        })
        .catch((error: unknown) => {
          setEntityErrorMessage(resolveEntityErrorMessage(error));
        });
    });
  };

  if (isLoading) {
    return (
      <div className="app-shell entity-workspace-loading">
        <SurfaceCard title="Loading Workspace" subtitle="Entity detail">
          <p className="form-helper">Loading workspace settings, memberships, and activity...</p>
        </SurfaceCard>
      </div>
    );
  }

  if (entity === null || settingsFormState === null) {
    return (
      <div className="app-shell entity-workspace-loading">
        <SurfaceCard title="Workspace Unavailable" subtitle="Entity detail">
          <div className="status-banner danger" role="alert">
            {entityErrorMessage ?? "The requested workspace could not be loaded."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  return (
    <div className="app-shell entity-workspace-page">
      <section className="hero-grid entity-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Entity Workspace</p>
          <h1>{entity.name}</h1>
          <p className="lede">
            Keep workspace defaults, operator ownership, and the entity activity stream visible in
            one place before close runs start accumulating documents and approvals.
          </p>
          <div className="coa-hero-actions">
            <Link className="secondary-button" href={`/entities/${entity.id}/coa`}>
              Open chart of accounts
            </Link>
            {entity.current_user_membership.role === "owner" ? (
              <button
                className="secondary-button"
                disabled={isPending}
                onClick={handleDeleteWorkspace}
                type="button"
              >
                {isPending ? "Working..." : "Delete workspace"}
              </button>
            ) : null}
          </div>
        </div>

        <SurfaceCard title="Workspace Snapshot" subtitle="Current defaults" tone="accent">
          <dl className="entity-meta-grid workspace-snapshot-grid">
            <div>
              <dt>Base currency</dt>
              <dd>{entity.base_currency}</dd>
            </div>
            <div>
              <dt>Language</dt>
              <dd>{entity.workspace_language.toUpperCase()}</dd>
            </div>
            <div>
              <dt>Default actor</dt>
              <dd>{entity.default_actor?.full_name ?? "Unassigned"}</dd>
            </div>
            <div>
              <dt>Autonomy mode</dt>
              <dd>{entity.autonomy_mode.replace("_", " ")}</dd>
            </div>
          </dl>
        </SurfaceCard>
      </section>

      {entityErrorMessage ? (
        <div className="status-banner danger" role="alert">
          {entityErrorMessage}
        </div>
      ) : null}

      <section className="entity-workspace-grid">
        <SurfaceCard title="Workspace Settings" subtitle="Update defaults">
          <form className="entity-form" onSubmit={handleEntityUpdate}>
            <label className="field">
              <span>Workspace name</span>
              <input
                className="text-input"
                onChange={handleSettingsFieldChange("name")}
                required
                type="text"
                value={settingsFormState.name}
              />
            </label>

            <label className="field">
              <span>Legal name</span>
              <input
                className="text-input"
                onChange={handleSettingsFieldChange("legalName")}
                type="text"
                value={settingsFormState.legalName}
              />
            </label>

            <div className="entity-form-row">
              <label className="field">
                <span>Base currency</span>
                <input
                  className="text-input"
                  maxLength={3}
                  onChange={handleSettingsFieldChange("baseCurrency")}
                  required
                  type="text"
                  value={settingsFormState.baseCurrency}
                />
              </label>
              <label className="field">
                <span>Country code</span>
                <input
                  className="text-input"
                  maxLength={2}
                  onChange={handleSettingsFieldChange("countryCode")}
                  required
                  type="text"
                  value={settingsFormState.countryCode}
                />
              </label>
            </div>

            <label className="field">
              <span>Timezone</span>
              <input
                className="text-input"
                onChange={handleSettingsFieldChange("timezone")}
                required
                type="text"
                value={settingsFormState.timezone}
              />
            </label>

            <div className="entity-form-row">
              <label className="field">
                <span>Accounting standard</span>
                <input
                  className="text-input"
                  onChange={handleSettingsFieldChange("accountingStandard")}
                  type="text"
                  value={settingsFormState.accountingStandard}
                />
              </label>
              <label className="field">
                <span>Autonomy mode</span>
                <select
                  className="text-input"
                  onChange={handleSettingsFieldChange("autonomyMode")}
                  value={settingsFormState.autonomyMode}
                >
                  <option value="human_review">Human review</option>
                  <option value="reduced_interruption">Reduced interruption</option>
                </select>
              </label>
            </div>

            <button className="primary-button" disabled={isPending} type="submit">
              {isPending ? "Saving settings..." : "Save settings"}
            </button>
          </form>
        </SurfaceCard>

        <SurfaceCard title="Memberships" subtitle="Ownership and default actor">
          <form className="entity-form" onSubmit={handleAddMembership}>
            <label className="field">
              <span>Operator email</span>
              <input
                className="text-input"
                onChange={handleMembershipFieldChange("userEmail")}
                placeholder="reviewer@example.com"
                required
                type="email"
                value={membershipFormState.userEmail}
              />
            </label>

            <div className="entity-form-row">
              <label className="field">
                <span>Role</span>
                <input
                  className="text-input"
                  onChange={handleMembershipFieldChange("role")}
                  required
                  type="text"
                  value={membershipFormState.role}
                />
              </label>

              <label className="checkbox-field">
                <input
                  checked={membershipFormState.isDefaultActor}
                  onChange={handleMembershipFieldChange("isDefaultActor")}
                  type="checkbox"
                />
                <span>Make this operator the default actor</span>
              </label>
            </div>

            <button className="secondary-button" disabled={isPending} type="submit">
              {isPending ? "Adding member..." : "Add member"}
            </button>
          </form>

          <div className="membership-list">
            {entity.memberships.map((membership: EntityWorkspace["memberships"][number]) => (
              <article className="membership-card" key={membership.id}>
                <div className="membership-card-header">
                  <div>
                    <strong>{membership.user.full_name}</strong>
                    <span>{membership.user.email}</span>
                  </div>
                  {membership.is_default_actor ? (
                    <span className="entity-status-chip default-actor-chip">Default actor</span>
                  ) : null}
                </div>

                <div className="membership-card-actions">
                  <input
                    aria-label={`Role for ${membership.user.full_name}`}
                    className="text-input compact-input"
                    defaultValue={membership.role}
                    onBlur={(event) => {
                      const nextRole = event.target.value.trim().toLowerCase().replaceAll(" ", "_");
                      if (nextRole.length === 0 || nextRole === membership.role) {
                        event.target.value = membership.role;
                        return;
                      }

                      handleMembershipUpdate(membership.id, { role: nextRole });
                    }}
                    type="text"
                  />

                  {!membership.is_default_actor ? (
                    <button
                      className="secondary-button compact-button"
                      disabled={isPending}
                      onClick={() =>
                        handleMembershipUpdate(membership.id, { is_default_actor: true })
                      }
                      type="button"
                    >
                      Set default
                    </button>
                  ) : (
                    <button
                      className="secondary-button compact-button"
                      disabled={isPending}
                      onClick={() =>
                        handleMembershipUpdate(membership.id, { is_default_actor: false })
                      }
                      type="button"
                    >
                      Remove default
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        </SurfaceCard>

        <SurfaceCard title="Close Runs" subtitle="Period workflows">
          {closeRunErrorMessage ? (
            <div className="status-banner warning" role="status">
              {closeRunErrorMessage}
            </div>
          ) : null}

          <form className="entity-form close-run-create-form" onSubmit={handleCreateCloseRun}>
            <div className="entity-form-row">
              <label className="field">
                <span>Period start</span>
                <input
                  className="text-input"
                  onChange={handleCloseRunFieldChange("periodStart")}
                  required
                  type="date"
                  value={closeRunFormState.periodStart}
                />
              </label>
              <label className="field">
                <span>Period end</span>
                <input
                  className="text-input"
                  onChange={handleCloseRunFieldChange("periodEnd")}
                  required
                  type="date"
                  value={closeRunFormState.periodEnd}
                />
              </label>
            </div>

            <div className="entity-form-row">
              <label className="field">
                <span>Reporting currency</span>
                <input
                  className="text-input"
                  maxLength={3}
                  onChange={handleCloseRunFieldChange("reportingCurrency")}
                  type="text"
                  value={closeRunFormState.reportingCurrency}
                />
              </label>
              <div className="field close-run-create-actions">
                <span>New close run</span>
                <button className="primary-button" disabled={isPending} type="submit">
                  {isPending ? "Creating close run..." : "Create close run"}
                </button>
              </div>
            </div>
          </form>

          {closeRunErrorMessage === null && closeRuns.length === 0 ? (
            <p className="form-helper">
              No close runs exist yet for this workspace. Create the first period run here to begin
              the accounting workflow for this entity.
            </p>
          ) : (
            <div className="dashboard-row-list">
              {closeRuns.map((closeRun) => (
                <article className="dashboard-row" key={closeRun.id}>
                  <div className="close-run-row-header">
                    <div>
                      <strong className="close-run-row-title">
                        {formatCloseRunPeriod(closeRun)}
                      </strong>
                      <p className="close-run-row-meta">
                        {getCloseRunStatusLabel(closeRun.status)} • v{closeRun.currentVersionNo} •{" "}
                        {closeRun.reportingCurrency}
                      </p>
                    </div>
                    <span className="entity-status-chip">
                      {deriveCloseRunAttention(closeRun).label}
                    </span>
                  </div>

                  <p className="form-helper">{deriveCloseRunAttention(closeRun).detail}</p>

                  <div className="close-run-link-row">
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entity.id}/close-runs/${closeRun.id}`}
                    >
                      Overview
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entity.id}/close-runs/${closeRun.id}/documents`}
                    >
                      Documents
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entity.id}/close-runs/${closeRun.id}/reconciliation`}
                    >
                      Reconciliation
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entity.id}/close-runs/${closeRun.id}/schedules`}
                    >
                      Schedules
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entity.id}/close-runs/${closeRun.id}/chat`}
                    >
                      Agent
                    </Link>
                  </div>
                </article>
              ))}
            </div>
          )}
        </SurfaceCard>

        <SurfaceCard title="Agent Capability Catalog" subtitle="Workspace-level runtime map">
          <AgentCapabilityCatalog maxTools={6} />
        </SurfaceCard>
      </section>

      <section className="entity-activity-section">
        <SurfaceCard title="Activity Timeline" subtitle="Root event stream">
          <Timeline
            emptyMessage="Activity appears here when the workspace records uploads, approvals, and ownership changes."
            items={activityTimelineItems}
          />
        </SurfaceCard>
      </section>
    </div>
  );
}

async function loadWorkspaceView(options: {
  entityId: string;
  onCloseRunError: (message: string | null) => void;
  onCloseRunsLoaded: (closeRuns: readonly CloseRunSummary[]) => void;
  onError: (message: string | null) => void;
  onLoaded: (workspace: EntityWorkspace) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const workspace = await readEntityWorkspace(options.entityId);
    options.onLoaded(workspace);
    options.onError(null);

    try {
      const closeRuns = await listCloseRuns(options.entityId);
      options.onCloseRunsLoaded(closeRuns);
      options.onCloseRunError(null);
    } catch (error: unknown) {
      options.onCloseRunsLoaded([]);
      options.onCloseRunError(resolveWorkspaceViewErrorMessage(error));
    }
  } catch (error: unknown) {
    options.onCloseRunsLoaded([]);
    options.onCloseRunError(null);
    options.onError(resolveWorkspaceViewErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function emptyStringToNull(value: string): string | null {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function deriveSettingsFormState(entity: Readonly<EntityWorkspace>): EntitySettingsFormState {
  return {
    accountingStandard: entity.accounting_standard ?? "",
    autonomyMode: entity.autonomy_mode,
    baseCurrency: entity.base_currency,
    countryCode: entity.country_code,
    legalName: entity.legal_name ?? "",
    name: entity.name,
    timezone: entity.timezone,
  };
}

function resolveEntityErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError) {
    return error.message;
  }

  return "The entity workspace request failed. Reload the page and try again.";
}

function resolveWorkspaceViewErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError || error instanceof CloseRunApiError) {
    return error.message;
  }

  return "The entity workspace request failed. Reload the page and try again.";
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
