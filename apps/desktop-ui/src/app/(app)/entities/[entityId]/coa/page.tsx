/*
Purpose: Render the entity chart-of-accounts workspace with Quartz-aligned upload, activation, and account editing flows.
Scope: Client-side COA reads plus versioned account creation/update actions through same-origin APIs.
Dependencies: React hooks, route params, Next links, and the COA API helper module.
*/

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import {
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
  CoaApiError,
  activateCoaSet,
  createCoaAccount,
  readCoaWorkspace,
  updateCoaAccount,
  uploadManualCoa,
  type CoaAccountSummary,
  type CoaCreateAccountRequest,
  type CoaSetSummary,
  type CoaWorkspaceResponse,
} from "../../../../../lib/coa";
import { requireRouteParam } from "../../../../../lib/route-params";

type CreateAccountFormState = {
  accountCode: string;
  accountName: string;
  accountType: string;
  externalRef: string;
  isActive: boolean;
  isPostable: boolean;
  parentAccountId: string;
};

type AccountDraft = {
  accountCode: string;
  accountName: string;
  accountType: string;
  externalRef: string;
  isActive: boolean;
  isPostable: boolean;
  parentAccountId: string;
};

const defaultCreateAccountFormState: CreateAccountFormState = {
  accountCode: "",
  accountName: "",
  accountType: "expense",
  externalRef: "",
  isActive: true,
  isPostable: true,
  parentAccountId: "",
};

/**
 * Purpose: Render one entity COA workspace with upload, activation, and editor controls.
 * Inputs: Route params containing the target entity UUID.
 * Outputs: A client-rendered COA workspace page with set versions and account editor tools.
 * Behavior: Keeps all COA mutations routed through versioned backend workflows and reloads on success.
 */
export default function EntityCoaPage(): ReactElement {
  const routeParams = useParams<{ entityId: string }>();
  const entityId = requireRouteParam(routeParams.entityId, "entityId");

  const [workspace, setWorkspace] = useState<CoaWorkspaceResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();
  const [createForm, setCreateForm] = useState<CreateAccountFormState>(
    defaultCreateAccountFormState,
  );
  const [accountDrafts, setAccountDrafts] = useState<Readonly<Record<string, AccountDraft>>>({});

  useEffect(() => {
    void loadWorkspace({
      entityId,
      onError: setErrorMessage,
      onLoaded: (nextWorkspace) => {
        setWorkspace(nextWorkspace);
        setAccountDrafts(buildAccountDrafts(nextWorkspace.accounts));
      },
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

  const parentAccountOptions = useMemo(
    () => workspace?.accounts.filter((account) => account.is_active) ?? [],
    [workspace],
  );

  const handleUploadFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    setUploadError(null);
    setSelectedFile(event.target.files?.[0] ?? null);
  };

  const handleUpload = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (selectedFile === null) {
      setUploadError("Select a CSV or XLSX file to upload.");
      return;
    }

    startTransition(() => {
      void uploadManualCoa(entityId, selectedFile)
        .then((nextWorkspace) => {
          setWorkspace(nextWorkspace);
          setAccountDrafts(buildAccountDrafts(nextWorkspace.accounts));
          setSelectedFile(null);
          setUploadError(null);
          setErrorMessage(null);
        })
        .catch((error: unknown) => {
          setUploadError(resolveCoaErrorMessage(error));
        });
    });
  };

  const handleActivateSet = (coaSetId: string): void => {
    startTransition(() => {
      void activateCoaSet({ coaSetId, entityId })
        .then((nextWorkspace) => {
          setWorkspace(nextWorkspace);
          setAccountDrafts(buildAccountDrafts(nextWorkspace.accounts));
          setErrorMessage(null);
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveCoaErrorMessage(error));
        });
    });
  };

  const handleCreateFieldChange =
    (field: keyof CreateAccountFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      setCreateForm((current) => {
        switch (field) {
          case "accountCode":
            return { ...current, accountCode: event.target.value };
          case "accountName":
            return { ...current, accountName: event.target.value };
          case "accountType":
            return { ...current, accountType: event.target.value };
          case "externalRef":
            return { ...current, externalRef: event.target.value };
          case "parentAccountId":
            return { ...current, parentAccountId: event.target.value };
          case "isActive":
            return { ...current, isActive: (event.currentTarget as HTMLInputElement).checked };
          case "isPostable":
            return { ...current, isPostable: (event.currentTarget as HTMLInputElement).checked };
          default:
            return current;
        }
      });
    };

  const handleCreateAccount = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();

    const externalRef = emptyStringToUndefined(createForm.externalRef);
    const parentAccountId = emptyStringToUndefined(createForm.parentAccountId);
    const payload: CoaCreateAccountRequest = {
      account_code: createForm.accountCode,
      account_name: createForm.accountName,
      account_type: createForm.accountType,
      is_active: createForm.isActive,
      is_postable: createForm.isPostable,
      ...(externalRef ? { external_ref: externalRef } : {}),
      ...(parentAccountId ? { parent_account_id: parentAccountId } : {}),
    };

    startTransition(() => {
      void createCoaAccount(entityId, payload)
        .then((nextWorkspace) => {
          setWorkspace(nextWorkspace);
          setAccountDrafts(buildAccountDrafts(nextWorkspace.accounts));
          setCreateForm(defaultCreateAccountFormState);
          setErrorMessage(null);
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveCoaErrorMessage(error));
        });
    });
  };

  const handleDraftFieldChange = (
    accountId: string,
    field: keyof AccountDraft,
    value: string | boolean,
  ): void => {
    setAccountDrafts((currentDrafts) => {
      const currentDraft = currentDrafts[accountId];
      if (currentDraft === undefined) {
        return currentDrafts;
      }

      return {
        ...currentDrafts,
        [accountId]: {
          ...currentDraft,
          [field]: value,
        },
      };
    });
  };

  const handleSaveDraft = (account: CoaAccountSummary): void => {
    const draft = accountDrafts[account.id];
    if (draft === undefined) {
      return;
    }

    const payload = buildUpdatePayload(account, draft);
    if (Object.keys(payload).length === 0) {
      return;
    }

    startTransition(() => {
      void updateCoaAccount(entityId, account.id, payload)
        .then((nextWorkspace) => {
          setWorkspace(nextWorkspace);
          setAccountDrafts(buildAccountDrafts(nextWorkspace.accounts));
          setErrorMessage(null);
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveCoaErrorMessage(error));
        });
    });
  };

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading chart of accounts workspace...</div>
        </section>
      </div>
    );
  }

  if (workspace === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The chart-of-accounts workspace could not be loaded."}
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
            <h1>Chart of Accounts</h1>
            <p className="quartz-page-subtitle">
              Manage the versioned COA workspace used for mapped, review-safe accounting output.
              Manual uploads and account edits both materialize auditable revisions.
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/settings`}
            >
              <QuartzIcon className="quartz-inline-icon" name="settings" />
              Workspace Settings
            </Link>
            <Link className="secondary-button quartz-toolbar-button" href={`/entities/${entityId}`}>
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Entity Home
            </Link>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner danger quartz-section" role="alert">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid">
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Active Source</p>
              <p className="quartz-kpi-value">{workspace.active_set.source.replaceAll("_", " ")}</p>
              <p className="quartz-kpi-meta">Current precedence result</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Active Version</p>
              <p className="quartz-kpi-value">v{workspace.active_set.version_no}</p>
              <p className="quartz-kpi-meta">
                Activated{" "}
                {formatDateTime(
                  workspace.active_set.activated_at ?? workspace.active_set.created_at,
                )}
              </p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Accounts</p>
              <p className="quartz-kpi-value">{workspace.active_set.account_count}</p>
              <p className="quartz-kpi-meta">Accounts in the active COA set</p>
            </article>
            <article className="quartz-kpi-tile highlight">
              <p className="quartz-kpi-label">Precedence</p>
              <p className="quartz-kpi-value quartz-kpi-value-small">
                {workspace.precedence_order.join(" -> ")}
              </p>
              <p className="quartz-kpi-meta">Deterministic source ordering</p>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Manual Import</p>
                  <h2 className="quartz-section-title">Upload COA</h2>
                </div>
              </div>
              <form className="quartz-settings-form" onSubmit={handleUpload}>
                <label className="quartz-form-label">
                  <span>COA File</span>
                  <input
                    accept=".csv,.xlsx,.xlsm"
                    className="text-input"
                    onChange={handleUploadFileChange}
                    type="file"
                  />
                </label>

                {uploadError ? (
                  <div className="status-banner warning" role="alert">
                    {uploadError}
                  </div>
                ) : null}

                <div className="quartz-button-row">
                  <button className="primary-button" disabled={isPending} type="submit">
                    {isPending ? "Uploading COA..." : "Upload COA"}
                  </button>
                </div>
              </form>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Account Editor</p>
                  <h2 className="quartz-section-title">Create Account</h2>
                </div>
              </div>
              <form className="quartz-settings-form" onSubmit={handleCreateAccount}>
                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Account Code</span>
                    <input
                      className="text-input"
                      onChange={handleCreateFieldChange("accountCode")}
                      required
                      type="text"
                      value={createForm.accountCode}
                    />
                  </label>
                  <label className="quartz-form-label">
                    <span>Account Type</span>
                    <input
                      className="text-input"
                      onChange={handleCreateFieldChange("accountType")}
                      required
                      type="text"
                      value={createForm.accountType}
                    />
                  </label>
                </div>

                <label className="quartz-form-label">
                  <span>Account Name</span>
                  <input
                    className="text-input"
                    onChange={handleCreateFieldChange("accountName")}
                    required
                    type="text"
                    value={createForm.accountName}
                  />
                </label>

                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Parent Account</span>
                    <select
                      className="text-input"
                      onChange={handleCreateFieldChange("parentAccountId")}
                      value={createForm.parentAccountId}
                    >
                      <option value="">No parent</option>
                      {parentAccountOptions.map((account) => (
                        <option key={account.id} value={account.id}>
                          {account.account_code} · {account.account_name}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="quartz-form-label">
                    <span>External Reference</span>
                    <input
                      className="text-input"
                      onChange={handleCreateFieldChange("externalRef")}
                      type="text"
                      value={createForm.externalRef}
                    />
                  </label>
                </div>

                <div className="quartz-form-grid">
                  <label className="quartz-settings-checkbox">
                    <input
                      checked={createForm.isPostable}
                      onChange={handleCreateFieldChange("isPostable")}
                      type="checkbox"
                    />
                    <span>Postable account</span>
                  </label>
                  <label className="quartz-settings-checkbox">
                    <input
                      checked={createForm.isActive}
                      onChange={handleCreateFieldChange("isActive")}
                      type="checkbox"
                    />
                    <span>Active account</span>
                  </label>
                </div>

                <div className="quartz-button-row">
                  <button className="secondary-button" disabled={isPending} type="submit">
                    {isPending ? "Creating account..." : "Create Account"}
                  </button>
                </div>
              </form>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-settings-layout">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Versioning</p>
                  <h2 className="quartz-section-title">COA Versions</h2>
                </div>
              </div>
              <div className="quartz-summary-list">
                {workspace.coa_sets.map((coaSet: CoaSetSummary) => (
                  <div className="quartz-summary-row" key={coaSet.id}>
                    <div>
                      <strong>Version {coaSet.version_no}</strong>
                      <div className="quartz-table-secondary">
                        {coaSet.source.replaceAll("_", " ")} • {coaSet.account_count} accounts
                      </div>
                    </div>
                    <div className="quartz-inline-actions">
                      <span
                        className={`quartz-status-badge ${coaSet.is_active ? "success" : "neutral"}`}
                      >
                        {coaSet.is_active ? "Active" : "Inactive"}
                      </span>
                      {!coaSet.is_active ? (
                        <button
                          className="secondary-button quartz-inline-button"
                          disabled={isPending}
                          onClick={() => handleActivateSet(coaSet.id)}
                          type="button"
                        >
                          Activate
                        </button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Guardrails</p>
                  <h2 className="quartz-section-title">COA Notes</h2>
                </div>
              </div>
              <div className="quartz-settings-info-stack">
                <div className="quartz-compact-pill">Uploads create immutable COA versions</div>
                <div className="quartz-compact-pill">Activation is explicit and auditable</div>
                <div className="quartz-compact-pill">Inline edits stay version-safe</div>
              </div>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <article className="quartz-card quartz-card-table-shell">
            <div className="quartz-section-header">
              <div>
                <h2 className="quartz-section-title">Active Set Accounts</h2>
                <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                  Inline versioned editing for the active chart of accounts.
                </p>
              </div>
            </div>
            <div className="coa-table-container quartz-table-shell">
              <table className="quartz-table coa-table">
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Parent</th>
                    <th>Postable</th>
                    <th>Active</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {workspace.accounts.map((account) => {
                    const draft = accountDrafts[account.id];
                    if (draft === undefined) {
                      return null;
                    }

                    return (
                      <tr key={account.id}>
                        <td>
                          <input
                            className="text-input compact-input"
                            onChange={(event) =>
                              handleDraftFieldChange(account.id, "accountCode", event.target.value)
                            }
                            type="text"
                            value={draft.accountCode}
                          />
                        </td>
                        <td>
                          <input
                            className="text-input compact-input"
                            onChange={(event) =>
                              handleDraftFieldChange(account.id, "accountName", event.target.value)
                            }
                            type="text"
                            value={draft.accountName}
                          />
                        </td>
                        <td>
                          <input
                            className="text-input compact-input"
                            onChange={(event) =>
                              handleDraftFieldChange(account.id, "accountType", event.target.value)
                            }
                            type="text"
                            value={draft.accountType}
                          />
                        </td>
                        <td>
                          <select
                            className="text-input compact-input"
                            onChange={(event) =>
                              handleDraftFieldChange(
                                account.id,
                                "parentAccountId",
                                event.target.value,
                              )
                            }
                            value={draft.parentAccountId}
                          >
                            <option value="">No parent</option>
                            {workspace.accounts
                              .filter((candidate) => candidate.id !== account.id)
                              .map((candidate) => (
                                <option key={candidate.id} value={candidate.id}>
                                  {candidate.account_code} · {candidate.account_name}
                                </option>
                              ))}
                          </select>
                        </td>
                        <td className="quartz-table-center">
                          <label className="quartz-settings-checkbox quartz-settings-checkbox-inline">
                            <input
                              checked={draft.isPostable}
                              onChange={(event) =>
                                handleDraftFieldChange(
                                  account.id,
                                  "isPostable",
                                  event.target.checked,
                                )
                              }
                              type="checkbox"
                            />
                            <span>{draft.isPostable ? "Yes" : "No"}</span>
                          </label>
                        </td>
                        <td className="quartz-table-center">
                          <label className="quartz-settings-checkbox quartz-settings-checkbox-inline">
                            <input
                              checked={draft.isActive}
                              onChange={(event) =>
                                handleDraftFieldChange(account.id, "isActive", event.target.checked)
                              }
                              type="checkbox"
                            />
                            <span>{draft.isActive ? "Yes" : "No"}</span>
                          </label>
                        </td>
                        <td className="quartz-table-center">
                          <button
                            className="secondary-button quartz-inline-button"
                            disabled={isPending}
                            onClick={() => handleSaveDraft(account)}
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
          </article>
        </section>
      </section>
    </div>
  );
}

/**
 * Purpose: Load COA workspace state for the current entity route.
 * Inputs: Entity UUID and page-level state update callbacks.
 * Outputs: None; callers receive state through callbacks.
 * Behavior: Produces operator-safe error messages while preserving fail-fast backend diagnostics.
 */
async function loadWorkspace(options: {
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: CoaWorkspaceResponse) => void;
  onLoadingChange: (isLoading: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  options.onError(null);
  try {
    const workspace = await readCoaWorkspace(options.entityId);
    options.onLoaded(workspace);
  } catch (error: unknown) {
    options.onError(resolveCoaErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

/**
 * Purpose: Seed editable account-row drafts from the latest workspace payload.
 * Inputs: Read-only account rows from the active COA set.
 * Outputs: Mutable draft map keyed by account UUID.
 * Behavior: Keeps UI edits isolated until users explicitly save a revision.
 */
function buildAccountDrafts(
  accounts: readonly CoaAccountSummary[],
): Readonly<Record<string, AccountDraft>> {
  return Object.fromEntries(
    accounts.map((account) => [
      account.id,
      {
        accountCode: account.account_code,
        accountName: account.account_name,
        accountType: account.account_type,
        externalRef: account.external_ref ?? "",
        isActive: account.is_active,
        isPostable: account.is_postable,
        parentAccountId: account.parent_account_id ?? "",
      },
    ]),
  );
}

/**
 * Purpose: Derive PATCH payload fields by diffing one row draft against source data.
 * Inputs: Source account row and mutable draft values.
 * Outputs: Partial update payload containing only changed fields.
 * Behavior: Avoids empty patch requests and preserves backend fail-fast validation for real changes.
 */
function buildUpdatePayload(
  sourceAccount: CoaAccountSummary,
  draft: AccountDraft,
): Record<string, string | boolean | undefined> {
  const payload: Record<string, string | boolean | undefined> = {};

  if (draft.accountCode !== sourceAccount.account_code) {
    payload.account_code = draft.accountCode;
  }
  if (draft.accountName !== sourceAccount.account_name) {
    payload.account_name = draft.accountName;
  }
  if (draft.accountType !== sourceAccount.account_type) {
    payload.account_type = draft.accountType;
  }
  if (draft.parentAccountId !== (sourceAccount.parent_account_id ?? "")) {
    payload.parent_account_id = emptyStringToUndefined(draft.parentAccountId);
  }
  if (draft.externalRef !== (sourceAccount.external_ref ?? "")) {
    payload.external_ref = emptyStringToUndefined(draft.externalRef);
  }
  if (draft.isPostable !== sourceAccount.is_postable) {
    payload.is_postable = draft.isPostable;
  }
  if (draft.isActive !== sourceAccount.is_active) {
    payload.is_active = draft.isActive;
  }

  return payload;
}

function emptyStringToUndefined(value: string): string | undefined {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : undefined;
}

function resolveCoaErrorMessage(error: unknown): string {
  if (error instanceof CoaApiError) {
    return error.message;
  }

  return "The chart-of-accounts request failed. Reload and try again.";
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
