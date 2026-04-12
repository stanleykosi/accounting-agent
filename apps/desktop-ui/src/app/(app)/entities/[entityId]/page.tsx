/*
Purpose: Render one authenticated entity workspace overview with settings, memberships, and activity history.
Scope: Client-side workspace reads plus update and membership-management flows against the same-origin entity proxy.
Dependencies: React hooks, Next.js route params, the entity API helpers, and shared UI surface cards.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  use,
  useEffect,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import {
  EntityApiError,
  createEntityMembership,
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

const defaultMembershipFormState: AddMembershipFormState = {
  isDefaultActor: false,
  role: "member",
  userEmail: "",
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
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();
  const [membershipFormState, setMembershipFormState] = useState<AddMembershipFormState>(
    defaultMembershipFormState,
  );
  const [settingsFormState, setSettingsFormState] = useState<EntitySettingsFormState | null>(null);

  useEffect(() => {
    void loadWorkspace({
      entityId,
      onError: setEntityErrorMessage,
      onLoaded: (workspace) => {
        setEntity(workspace);
        setSettingsFormState(deriveSettingsFormState(workspace));
      },
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

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
      </section>

      <section className="entity-activity-section">
        <SurfaceCard title="Activity Timeline" subtitle="Root event stream">
          <div className="activity-list">
            {entity.activity_events.map(
              (activityEvent: EntityWorkspace["activity_events"][number]) => (
                <article className="activity-card" key={activityEvent.id}>
                  <div className="activity-card-header">
                    <strong>{activityEvent.summary}</strong>
                    <span>{formatDateTime(activityEvent.created_at)}</span>
                  </div>
                  <p>
                    {activityEvent.actor?.full_name ?? "System"} via {activityEvent.source_surface}
                    {activityEvent.trace_id ? ` - trace ${activityEvent.trace_id}` : ""}
                  </p>
                </article>
              ),
            )}
          </div>
        </SurfaceCard>
      </section>
    </div>
  );
}

async function loadWorkspace(options: {
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: EntityWorkspace) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const workspace = await readEntityWorkspace(options.entityId);
    options.onLoaded(workspace);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveEntityErrorMessage(error));
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

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
