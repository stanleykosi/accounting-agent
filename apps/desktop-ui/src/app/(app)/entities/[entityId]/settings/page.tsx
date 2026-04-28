"use client";

import { autonomyModeDefinitions, type AutonomyMode } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../../../../../components/layout/QuartzIcons";
import {
  EntityApiError,
  createEntityMembership,
  deleteEntityWorkspace,
  readEntityWorkspace,
  readEntityWorkspaceSnapshot,
  updateEntity,
  updateEntityMembership,
  type EntityWorkspace,
} from "../../../../../lib/entities/api";
import {
  accountingStandardOptions,
  commonWorkspaceRoleOptions,
  countryOptions,
  timezoneOptions,
} from "../../../../../lib/entities/options";
import { requireRouteParam } from "../../../../../lib/route-params";

type WorkspaceGeneralFormState = {
  accountingStandard: string;
  autonomyMode: AutonomyMode;
  baseCurrency: string;
  countryCode: string;
  legalName: string;
  name: string;
  timezone: string;
};

type AddMemberFormState = {
  isDefaultActor: boolean;
  role: string;
  userEmail: string;
};

type MembershipDraft = {
  isDefaultActor: boolean;
  role: string;
};

const defaultAddMemberFormState: AddMemberFormState = {
  isDefaultActor: false,
  role: "reviewer",
  userEmail: "",
};

const moduleDefinitions = [
  {
    description: "Upload, version, and activate the chart of accounts used across the workspace.",
    hrefSuffix: "/coa",
    title: "Chart of Accounts",
  },
  {
    description: "Manage imported general ledger and trial balance baselines for this entity.",
    hrefSuffix: "/ledger",
    title: "Imported Ledger",
  },
  {
    description: "Connect QuickBooks Online and run the chart-of-accounts sync workflow.",
    hrefSuffix: "/integrations",
    title: "Integrations",
  },
  {
    description: "Configure the report template pack used for reporting and commentary.",
    hrefSuffix: "/reports/templates",
    title: "Report Templates",
  },
] as const;

export default function WorkspaceSettingsPage(): ReactElement {
  const routeParams = useParams<{ entityId: string }>();
  const entityId = requireRouteParam(routeParams.entityId, "entityId");
  const router = useRouter();
  const entitySnapshot = readEntityWorkspaceSnapshot(entityId);
  const [entity, setEntity] = useState<EntityWorkspace | null>(entitySnapshot);
  const [generalFormState, setGeneralFormState] = useState<WorkspaceGeneralFormState>(() =>
    buildGeneralFormState(entitySnapshot),
  );
  const [membershipDrafts, setMembershipDrafts] = useState<
    Readonly<Record<string, MembershipDraft>>
  >(() => buildMembershipDrafts(entitySnapshot));
  const [addMemberFormState, setAddMemberFormState] =
    useState<AddMemberFormState>(defaultAddMemberFormState);
  const [deleteConfirmationValue, setDeleteConfirmationValue] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(() => entitySnapshot === null);
  const [isPending, startTransition] = useTransition();

  const applyWorkspace = useCallback((nextWorkspace: EntityWorkspace): void => {
    setEntity(nextWorkspace);
    setGeneralFormState(buildGeneralFormState(nextWorkspace));
    setMembershipDrafts(buildMembershipDrafts(nextWorkspace));
  }, []);

  useEffect(() => {
    void loadWorkspaceSettings({
      entityId,
      onError: setErrorMessage,
      onLoaded: applyWorkspace,
      onLoadingChange: setIsLoading,
      showLoading: entitySnapshot === null,
    });
  }, [applyWorkspace, entityId, entitySnapshot]);

  const currentUserIsOwner = entity?.current_user_membership.role === "owner";
  const moduleCards = useMemo(
    () =>
      moduleDefinitions.map((module) => ({
        ...module,
        href: `/entities/${entityId}${module.hrefSuffix}`,
      })),
    [entityId],
  );

  const defaultActorName = entity?.default_actor?.full_name ?? "Default actor not assigned";
  const activeRoleOptions = useMemo(
    () => buildWorkspaceRoleOptions(entity?.memberships.map((membership) => membership.role) ?? []),
    [entity],
  );

  const handleGeneralFieldChange =
    (fieldName: keyof WorkspaceGeneralFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      const nextValue = event.target.value;
      setGeneralFormState((currentState) => ({
        ...currentState,
        [fieldName]: nextValue,
        ...(fieldName === "countryCode"
          ? {
              timezone:
                countryOptions.find((option) => option.code === nextValue)?.timezone ??
                currentState.timezone,
            }
          : {}),
      }));
    };

  const handleGeneralSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (entity === null) {
      return;
    }

    setErrorMessage(null);
    setStatusMessage(null);

    startTransition(() => {
      void updateEntity(entityId, {
        accounting_standard: emptyStringToNull(generalFormState.accountingStandard),
        autonomy_mode: generalFormState.autonomyMode,
        base_currency: generalFormState.baseCurrency,
        country_code: generalFormState.countryCode,
        legal_name: emptyStringToNull(generalFormState.legalName),
        name: generalFormState.name.trim(),
        timezone: generalFormState.timezone,
      })
        .then((nextWorkspace) => {
          applyWorkspace(nextWorkspace);
          setStatusMessage("Workspace settings saved.");
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveWorkspaceSettingsError(error));
        });
    });
  };

  const handleAddMemberFieldChange =
    (fieldName: keyof AddMemberFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      const nextValue =
        fieldName === "isDefaultActor"
          ? (event.currentTarget as HTMLInputElement).checked
          : event.target.value;

      setAddMemberFormState((currentState) => ({
        ...currentState,
        [fieldName]: nextValue,
      }));
    };

  const handleAddMember = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setErrorMessage(null);
    setStatusMessage(null);

    startTransition(() => {
      void createEntityMembership(entityId, {
        is_default_actor: addMemberFormState.isDefaultActor,
        role: addMemberFormState.role,
        user_email: addMemberFormState.userEmail.trim(),
      })
        .then((nextWorkspace) => {
          applyWorkspace(nextWorkspace);
          setAddMemberFormState(defaultAddMemberFormState);
          setStatusMessage("Workspace member added.");
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveWorkspaceSettingsError(error));
        });
    });
  };

  const handleMembershipDraftChange = (
    membershipId: string,
    fieldName: keyof MembershipDraft,
    value: string | boolean,
  ): void => {
    setMembershipDrafts((currentDrafts) => {
      const currentDraft = currentDrafts[membershipId];
      if (currentDraft === undefined) {
        return currentDrafts;
      }

      return {
        ...currentDrafts,
        [membershipId]: {
          ...currentDraft,
          [fieldName]: value,
        },
      };
    });
  };

  const handleSaveMembership = (membershipId: string): void => {
    const draft = membershipDrafts[membershipId];
    if (draft === undefined) {
      return;
    }

    setErrorMessage(null);
    setStatusMessage(null);

    startTransition(() => {
      void updateEntityMembership(entityId, membershipId, {
        is_default_actor: draft.isDefaultActor,
        role: draft.role,
      })
        .then((nextWorkspace) => {
          applyWorkspace(nextWorkspace);
          setStatusMessage("Workspace membership updated.");
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveWorkspaceSettingsError(error));
        });
    });
  };

  const handleDeleteWorkspace = (): void => {
    if (entity === null || deleteConfirmationValue.trim() !== entity.name) {
      return;
    }

    setErrorMessage(null);
    setStatusMessage(null);

    startTransition(() => {
      void deleteEntityWorkspace(entity.id)
        .then(() => {
          router.push("/entities");
          router.refresh();
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveWorkspaceSettingsError(error));
        });
    });
  };

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading workspace settings...</div>
        </section>
      </div>
    );
  }

  if (entity === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The workspace settings page could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Workspace Settings</h1>
            <p className="quartz-page-subtitle">
              {entity.name} • Manage entity defaults, team access, connected modules, and the
              irreversible workspace lifecycle.
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <Link className="secondary-button quartz-toolbar-button" href={`/entities/${entityId}`}>
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Entity Home
            </Link>
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/assistant`}
            >
              <QuartzIcon className="quartz-inline-icon" name="assistant" />
              Entity Assistant
            </Link>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner danger quartz-section" role="alert">
            {errorMessage}
          </div>
        ) : null}

        {statusMessage ? (
          <div className="status-banner success quartz-section" role="status">
            {statusMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-settings-layout">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">General</p>
                  <h2 className="quartz-section-title">Workspace Defaults</h2>
                </div>
              </div>

              <form className="quartz-settings-form" onSubmit={handleGeneralSubmit}>
                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Workspace Name</span>
                    <input
                      className="text-input"
                      onChange={handleGeneralFieldChange("name")}
                      required
                      type="text"
                      value={generalFormState.name}
                    />
                  </label>

                  <label className="quartz-form-label">
                    <span>Legal Entity Name</span>
                    <input
                      className="text-input"
                      onChange={handleGeneralFieldChange("legalName")}
                      type="text"
                      value={generalFormState.legalName}
                    />
                  </label>
                </div>

                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Base Currency</span>
                    <select
                      className="text-input"
                      onChange={handleGeneralFieldChange("baseCurrency")}
                      value={generalFormState.baseCurrency}
                    >
                      <option value="NGN">NGN - Nigerian Naira</option>
                      <option value="USD">USD - US Dollar</option>
                      <option value="EUR">EUR - Euro</option>
                      <option value="GBP">GBP - British Pound</option>
                      <option value="KES">KES - Kenyan Shilling</option>
                      <option value="ZAR">ZAR - South African Rand</option>
                      <option value="AED">AED - UAE Dirham</option>
                    </select>
                  </label>

                  <label className="quartz-form-label">
                    <span>Accounting Standard</span>
                    <select
                      className="text-input"
                      onChange={handleGeneralFieldChange("accountingStandard")}
                      value={generalFormState.accountingStandard}
                    >
                      <option value="">Select accounting standard</option>
                      {accountingStandardOptions.map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Country</span>
                    <select
                      className="text-input"
                      onChange={handleGeneralFieldChange("countryCode")}
                      value={generalFormState.countryCode}
                    >
                      {countryOptions.map((option) => (
                        <option key={option.code} value={option.code}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="quartz-form-label">
                    <span>Timezone</span>
                    <select
                      className="text-input"
                      onChange={handleGeneralFieldChange("timezone")}
                      value={generalFormState.timezone}
                    >
                      {timezoneOptions.map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                <label className="quartz-form-label">
                  <span>Review Routing</span>
                  <select
                    className="text-input"
                    onChange={handleGeneralFieldChange("autonomyMode")}
                    value={generalFormState.autonomyMode}
                  >
                    {autonomyModeDefinitions.map((definition) => (
                      <option key={definition.code} value={definition.code}>
                        {definition.label}
                      </option>
                    ))}
                  </select>
                </label>

                <div className="quartz-button-row">
                  <button className="primary-button" disabled={isPending} type="submit">
                    {isPending ? "Saving..." : "Save Workspace Settings"}
                  </button>
                </div>
              </form>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Current Posture</p>
                  <h2 className="quartz-section-title">Workspace Summary</h2>
                </div>
              </div>

              <dl className="quartz-settings-summary-grid">
                <div className="quartz-settings-summary-row">
                  <dt>Status</dt>
                  <dd>{formatSentenceCase(entity.status)}</dd>
                </div>
                <div className="quartz-settings-summary-row">
                  <dt>Base currency</dt>
                  <dd>{entity.base_currency}</dd>
                </div>
                <div className="quartz-settings-summary-row">
                  <dt>Accounting standard</dt>
                  <dd>{entity.accounting_standard ?? "Not set"}</dd>
                </div>
                <div className="quartz-settings-summary-row">
                  <dt>Review routing</dt>
                  <dd>{formatAutonomyMode(entity.autonomy_mode)}</dd>
                </div>
                <div className="quartz-settings-summary-row">
                  <dt>Default actor</dt>
                  <dd>{defaultActorName}</dd>
                </div>
                <div className="quartz-settings-summary-row">
                  <dt>Current user role</dt>
                  <dd>{formatSentenceCase(entity.current_user_membership.role)}</dd>
                </div>
              </dl>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <div>
              <h2 className="quartz-section-title">Connected Modules</h2>
              <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                Use the dedicated workspaces for COA, ledger baselines, integrations, and report
                templates.
              </p>
            </div>
          </div>

          <div className="quartz-settings-module-grid">
            {moduleCards.map((module) => (
              <article className="quartz-card quartz-settings-module-card" key={module.title}>
                <p className="quartz-card-eyebrow">{module.title}</p>
                <p className="quartz-page-subtitle">{module.description}</p>
                <Link className="quartz-action-link" href={module.href}>
                  Open {module.title}
                </Link>
              </article>
            ))}
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <div>
              <h2 className="quartz-section-title">Team</h2>
              <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                Membership and default actor control stay at the workspace level.
              </p>
            </div>
          </div>

          <div className="quartz-split-grid quartz-settings-layout">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Add Member</p>
                  <h3>Invite an Existing Operator</h3>
                </div>
              </div>

              <form className="quartz-settings-form" onSubmit={handleAddMember}>
                <label className="quartz-form-label">
                  <span>Operator Email</span>
                  <input
                    className="text-input"
                    onChange={handleAddMemberFieldChange("userEmail")}
                    placeholder="name@company.com"
                    required
                    type="email"
                    value={addMemberFormState.userEmail}
                  />
                </label>

                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Role</span>
                    <select
                      className="text-input"
                      onChange={handleAddMemberFieldChange("role")}
                      value={addMemberFormState.role}
                    >
                      {activeRoleOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="quartz-settings-checkbox">
                    <input
                      checked={addMemberFormState.isDefaultActor}
                      onChange={handleAddMemberFieldChange("isDefaultActor")}
                      type="checkbox"
                    />
                    <span>Make default actor</span>
                  </label>
                </div>

                <div className="quartz-button-row">
                  <button className="primary-button" disabled={isPending} type="submit">
                    {isPending ? "Adding..." : "Add Member"}
                  </button>
                </div>
              </form>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Team Notes</p>
                  <h3>Governed Membership Rules</h3>
                </div>
              </div>
              <div className="quartz-settings-info-stack">
                <div className="quartz-compact-pill">Exactly one default actor is preserved</div>
                <div className="quartz-compact-pill">
                  Only existing local operators can be added
                </div>
                <div className="quartz-compact-pill">Workspace deletion remains owner-only</div>
              </div>
            </article>
          </div>

          <div className="quartz-card quartz-card-table-shell">
            <div className="quartz-section-header">
              <h3 className="quartz-section-title">Current Memberships</h3>
              <span className="quartz-queue-meta">
                {entity.memberships.length} member{entity.memberships.length === 1 ? "" : "s"}
              </span>
            </div>
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Member</th>
                  <th>Role</th>
                  <th>Default Actor</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {entity.memberships.map((membership) => {
                  const draft = membershipDrafts[membership.id] ?? {
                    isDefaultActor: membership.is_default_actor,
                    role: membership.role,
                  };
                  const roleChanged = draft.role !== membership.role;
                  const defaultActorChanged = draft.isDefaultActor !== membership.is_default_actor;
                  const hasChanges = roleChanged || defaultActorChanged;

                  return (
                    <tr key={membership.id}>
                      <td>
                        <div className="quartz-table-primary">
                          {membership.user.full_name}
                          {membership.user.id === entity.current_user_membership.user.id
                            ? " (You)"
                            : ""}
                        </div>
                        <div className="quartz-table-secondary">{membership.user.email}</div>
                      </td>
                      <td>
                        <select
                          className="text-input quartz-inline-select"
                          onChange={(event) =>
                            handleMembershipDraftChange(membership.id, "role", event.target.value)
                          }
                          value={draft.role}
                        >
                          {buildWorkspaceRoleOptions([draft.role]).map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <label className="quartz-settings-checkbox quartz-settings-checkbox-inline">
                          <input
                            checked={draft.isDefaultActor}
                            onChange={(event) =>
                              handleMembershipDraftChange(
                                membership.id,
                                "isDefaultActor",
                                event.currentTarget.checked,
                              )
                            }
                            type="checkbox"
                          />
                          <span>{draft.isDefaultActor ? "Primary" : "Secondary"}</span>
                        </label>
                      </td>
                      <td className="quartz-table-center">
                        <button
                          className="secondary-button quartz-inline-button"
                          disabled={isPending || !hasChanges}
                          onClick={() => handleSaveMembership(membership.id)}
                          type="button"
                        >
                          Save
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <div>
              <h2 className="quartz-section-title">Danger Zone</h2>
              <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                Deleting a workspace removes its close runs, documents, chats, and linked jobs.
              </p>
            </div>
          </div>

          <article className="quartz-card quartz-settings-card quartz-settings-danger-card">
            {currentUserIsOwner ? (
              <>
                <p className="quartz-page-subtitle">
                  Type <strong>{entity.name}</strong> to confirm the irreversible workspace delete.
                </p>
                <label className="quartz-form-label">
                  <span>Confirm workspace name</span>
                  <input
                    className="text-input"
                    onChange={(event) => setDeleteConfirmationValue(event.target.value)}
                    type="text"
                    value={deleteConfirmationValue}
                  />
                </label>
                <div className="quartz-button-row">
                  <button
                    className="secondary-button quartz-danger-button"
                    disabled={isPending || deleteConfirmationValue.trim() !== entity.name}
                    onClick={handleDeleteWorkspace}
                    type="button"
                  >
                    {isPending ? "Deleting..." : "Delete Workspace"}
                  </button>
                </div>
              </>
            ) : (
              <div className="status-banner warning" role="status">
                Only workspace owners can delete an entity workspace.
              </div>
            )}
          </article>
        </section>
      </section>
    </div>
  );
}

async function loadWorkspaceSettings(options: {
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: EntityWorkspace) => void;
  onLoadingChange: (value: boolean) => void;
  showLoading: boolean;
}): Promise<void> {
  if (options.showLoading) {
    options.onLoadingChange(true);
  }

  try {
    const workspace = await readEntityWorkspace(options.entityId);
    options.onLoaded(workspace);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveWorkspaceSettingsError(error));
  } finally {
    if (options.showLoading) {
      options.onLoadingChange(false);
    }
  }
}

function buildGeneralFormState(entity: EntityWorkspace | null): WorkspaceGeneralFormState {
  return {
    accountingStandard: entity?.accounting_standard ?? "",
    autonomyMode: (entity?.autonomy_mode ?? "human_review") as AutonomyMode,
    baseCurrency: entity?.base_currency ?? "NGN",
    countryCode: entity?.country_code ?? "NG",
    legalName: entity?.legal_name ?? "",
    name: entity?.name ?? "",
    timezone: entity?.timezone ?? "Africa/Lagos",
  };
}

function buildMembershipDrafts(
  entity: EntityWorkspace | null,
): Readonly<Record<string, MembershipDraft>> {
  if (entity === null) {
    return {};
  }

  return Object.fromEntries(
    entity.memberships.map((membership) => [
      membership.id,
      {
        isDefaultActor: membership.is_default_actor,
        role: membership.role,
      },
    ]),
  );
}

function buildWorkspaceRoleOptions(currentRoles: readonly string[]): readonly {
  label: string;
  value: string;
}[] {
  const optionMap = new Map<string, { label: string; value: string }>(
    commonWorkspaceRoleOptions.map((option) => [option.value, option]),
  );

  currentRoles.forEach((role) => {
    if (!optionMap.has(role)) {
      optionMap.set(role, {
        label: formatSentenceCase(role.replaceAll("_", " ")),
        value: role,
      });
    }
  });

  return [...optionMap.values()];
}

function resolveWorkspaceSettingsError(error: unknown): string {
  if (error instanceof EntityApiError) {
    return error.message;
  }

  return "The workspace settings request could not be completed. Reload and try again.";
}

function formatAutonomyMode(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatSentenceCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function emptyStringToNull(value: string): string | null {
  const normalizedValue = value.trim();
  return normalizedValue.length > 0 ? normalizedValue : null;
}
