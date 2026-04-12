/*
Purpose: Render the entity chart-of-accounts workspace with upload, activation, and account editing flows.
Scope: Client-side COA reads plus versioned account creation/update actions through same-origin APIs.
Dependencies: React hooks, route params, shared SurfaceCard, and the COA API helper module.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import Link from "next/link";
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

type CoaPageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

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
export default function EntityCoaPage({ params }: Readonly<CoaPageProps>): ReactElement {
  const { entityId } = use(params);

  const [workspace, setWorkspace] = useState<CoaWorkspaceResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();
  const [createForm, setCreateForm] = useState<CreateAccountFormState>(defaultCreateAccountFormState);
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
      <div className="app-shell coa-page">
        <SurfaceCard title="Loading Chart of Accounts" subtitle="Entity COA workspace">
          <p className="form-helper">Loading chart-of-accounts sets, accounts, and version history...</p>
        </SurfaceCard>
      </div>
    );
  }

  if (workspace === null) {
    return (
      <div className="app-shell coa-page">
        <SurfaceCard title="COA Workspace Unavailable" subtitle="Entity COA workspace">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The chart-of-accounts workspace could not be loaded."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  return (
    <div className="app-shell coa-page">
      <section className="hero-grid coa-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Chart of Accounts</p>
          <h1>Versioned COA workspace for mapped, review-safe accounting output.</h1>
          <p className="lede">
            Manual uploads create new COA versions. Account edits also materialize immutable
            revisions so every code change and activation decision remains auditable.
          </p>
          <div className="coa-hero-actions">
            <Link className="secondary-button" href={`/entities/${entityId}`}>
              Back to entity workspace
            </Link>
          </div>
        </div>

        <SurfaceCard title="Active COA Set" subtitle="Current precedence result" tone="accent">
          <dl className="entity-meta-grid coa-summary-grid">
            <div>
              <dt>Source</dt>
              <dd>{workspace.active_set.source.replaceAll("_", " ")}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>v{workspace.active_set.version_no}</dd>
            </div>
            <div>
              <dt>Accounts</dt>
              <dd>{workspace.active_set.account_count}</dd>
            </div>
            <div>
              <dt>Activated</dt>
              <dd>{formatDateTime(workspace.active_set.activated_at ?? workspace.active_set.created_at)}</dd>
            </div>
          </dl>

          <p className="form-helper coa-precedence-label">
            Precedence: {workspace.precedence_order.join(" → ")}
          </p>
        </SurfaceCard>
      </section>

      {errorMessage ? (
        <div className="status-banner danger" role="alert">
          {errorMessage}
        </div>
      ) : null}

      <section className="coa-grid">
        <SurfaceCard title="Upload Manual COA" subtitle="CSV or XLSX import">
          <form className="entity-form" onSubmit={handleUpload}>
            <label className="field">
              <span>COA file</span>
              <input accept=".csv,.xlsx,.xlsm" onChange={handleUploadFileChange} type="file" />
            </label>

            {uploadError ? (
              <div className="status-banner warning" role="alert">
                {uploadError}
              </div>
            ) : null}

            <button className="primary-button" disabled={isPending} type="submit">
              {isPending ? "Uploading COA..." : "Upload COA"}
            </button>
          </form>
        </SurfaceCard>

        <SurfaceCard title="Create Account" subtitle="Versioned account editor">
          <form className="entity-form" onSubmit={handleCreateAccount}>
            <div className="entity-form-row">
              <label className="field">
                <span>Account code</span>
                <input
                  className="text-input"
                  onChange={handleCreateFieldChange("accountCode")}
                  required
                  type="text"
                  value={createForm.accountCode}
                />
              </label>

              <label className="field">
                <span>Account type</span>
                <input
                  className="text-input"
                  onChange={handleCreateFieldChange("accountType")}
                  required
                  type="text"
                  value={createForm.accountType}
                />
              </label>
            </div>

            <label className="field">
              <span>Account name</span>
              <input
                className="text-input"
                onChange={handleCreateFieldChange("accountName")}
                required
                type="text"
                value={createForm.accountName}
              />
            </label>

            <div className="entity-form-row">
              <label className="field">
                <span>Parent account</span>
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

              <label className="field">
                <span>External reference</span>
                <input
                  className="text-input"
                  onChange={handleCreateFieldChange("externalRef")}
                  type="text"
                  value={createForm.externalRef}
                />
              </label>
            </div>

            <div className="coa-toggle-row">
              <label className="checkbox-field">
                <input
                  checked={createForm.isPostable}
                  onChange={handleCreateFieldChange("isPostable")}
                  type="checkbox"
                />
                <span>Postable account</span>
              </label>
              <label className="checkbox-field">
                <input
                  checked={createForm.isActive}
                  onChange={handleCreateFieldChange("isActive")}
                  type="checkbox"
                />
                <span>Active account</span>
              </label>
            </div>

            <button className="secondary-button" disabled={isPending} type="submit">
              {isPending ? "Creating account..." : "Create account"}
            </button>
          </form>
        </SurfaceCard>
      </section>

      <section className="coa-grid coa-grid-wide">
        <SurfaceCard title="COA Versions" subtitle="Activation history">
          <div className="coa-set-list">
            {workspace.coa_sets.map((coaSet: CoaSetSummary) => (
              <article className="coa-set-card" key={coaSet.id}>
                <div>
                  <p className="eyebrow coa-set-eyebrow">{coaSet.source.replaceAll("_", " ")}</p>
                  <h3>Version {coaSet.version_no}</h3>
                  <p className="form-helper">{coaSet.account_count} accounts</p>
                </div>
                <div className="coa-set-actions">
                  <span className="entity-status-chip">
                    {coaSet.is_active ? "Active" : "Inactive"}
                  </span>
                  {!coaSet.is_active ? (
                    <button
                      className="secondary-button compact-button"
                      disabled={isPending}
                      onClick={() => handleActivateSet(coaSet.id)}
                      type="button"
                    >
                      Activate
                    </button>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        </SurfaceCard>

        <SurfaceCard title="Active Set Accounts" subtitle="Inline versioned editing">
          <div className="coa-table-container">
            <table className="coa-table">
              <thead>
                <tr>
                  <th>Code</th>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Parent</th>
                  <th>Postable</th>
                  <th>Active</th>
                  <th>Actions</th>
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
                            handleDraftFieldChange(account.id, "parentAccountId", event.target.value)
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
                      <td>
                        <input
                          checked={draft.isPostable}
                          onChange={(event) =>
                            handleDraftFieldChange(account.id, "isPostable", event.target.checked)
                          }
                          type="checkbox"
                        />
                      </td>
                      <td>
                        <input
                          checked={draft.isActive}
                          onChange={(event) =>
                            handleDraftFieldChange(account.id, "isActive", event.target.checked)
                          }
                          type="checkbox"
                        />
                      </td>
                      <td>
                        <button
                          className="secondary-button compact-button"
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
        </SurfaceCard>
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
function buildAccountDrafts(accounts: readonly CoaAccountSummary[]): Readonly<Record<string, AccountDraft>> {
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
