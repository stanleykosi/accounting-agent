/*
Purpose: Render the authenticated entity workspace directory for desktop operators.
Scope: Client-side entity listing, workspace creation, and quick navigation into individual workspaces.
Dependencies: React state hooks, Next.js navigation, the same-origin entity API helpers, and shared UI cards.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useEffect,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import {
  EntityApiError,
  createEntity,
  listEntities,
  type CreateEntityRequest,
  type EntitySummary,
} from "../../../lib/entities/api";

type CreateEntityFormState = {
  accountingStandard: string;
  autonomyMode: CreateEntityRequest["autonomy_mode"];
  baseCurrency: string;
  countryCode: string;
  legalName: string;
  name: string;
  timezone: string;
};

const defaultCreateEntityFormState: CreateEntityFormState = {
  accountingStandard: "",
  autonomyMode: "human_review",
  baseCurrency: "NGN",
  countryCode: "NG",
  legalName: "",
  name: "",
  timezone: "Africa/Lagos",
};

/**
 * Purpose: Render the entity workspace directory with creation controls and recent-activity summaries.
 * Inputs: None.
 * Outputs: A client-rendered page showing accessible workspaces and a create-workspace form.
 * Behavior: Fetches through the same-origin proxy so session rotation and auth cookies remain synchronized.
 */
export default function EntitiesPage(): ReactElement {
  const router = useRouter();
  const [entities, setEntities] = useState<readonly EntitySummary[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [formState, setFormState] = useState<CreateEntityFormState>(defaultCreateEntityFormState);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void loadEntities({
      onError: setErrorMessage,
      onLoaded: setEntities,
      onLoadingChange: setIsLoading,
    });
  }, []);

  const handleFieldChange =
    (fieldName: keyof CreateEntityFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      setFormState((currentState) => ({
        ...currentState,
        [fieldName]: event.target.value,
      }));
    };

  const handleCreateEntity = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setErrorMessage(null);

    startTransition(() => {
      void createEntity(buildCreateEntityPayload(formState))
        .then((workspace) => {
          setFormState(defaultCreateEntityFormState);
          router.push(`/entities/${workspace.id}`);
          router.refresh();
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveEntityErrorMessage(error));
        });
    });
  };

  return (
    <div className="app-shell entity-directory">
      <section className="hero-grid entity-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Entity Workspaces</p>
          <h1>One workspace per entity, with clear ownership and an auditable activity stream.</h1>
          <p className="lede">
            Use entity workspaces to anchor memberships, base-currency defaults, and the timeline
            that future close runs, documents, and approvals will build on.
          </p>
        </div>

        <SurfaceCard title="Workspace Defaults" subtitle="Step 15 foundation" tone="accent">
          <ul className="detail-list">
            <li>
              New workspaces default to Naira (NGN) with Africa/Lagos as the starting timezone.
            </li>
            <li>
              English is the current workspace language scope until broader locale settings land.
            </li>
            <li>
              Each workspace keeps exactly one default actor so approvals and ownership stay
              anchored.
            </li>
          </ul>
        </SurfaceCard>
      </section>

      <section className="entity-grid">
        <SurfaceCard title="Create Workspace" subtitle="New entity">
          <form className="entity-form" onSubmit={handleCreateEntity}>
            <label className="field">
              <span>Workspace name</span>
              <input
                className="text-input"
                name="name"
                onChange={handleFieldChange("name")}
                placeholder="Northwind Nigeria"
                required
                type="text"
                value={formState.name}
              />
            </label>

            <label className="field">
              <span>Legal name</span>
              <input
                className="text-input"
                name="legalName"
                onChange={handleFieldChange("legalName")}
                placeholder="Northwind Nigeria Limited"
                type="text"
                value={formState.legalName}
              />
            </label>

            <div className="entity-form-row">
              <label className="field">
                <span>Base currency</span>
                <input
                  className="text-input"
                  maxLength={3}
                  name="baseCurrency"
                  onChange={handleFieldChange("baseCurrency")}
                  required
                  type="text"
                  value={formState.baseCurrency}
                />
              </label>

              <label className="field">
                <span>Country code</span>
                <input
                  className="text-input"
                  maxLength={2}
                  name="countryCode"
                  onChange={handleFieldChange("countryCode")}
                  required
                  type="text"
                  value={formState.countryCode}
                />
              </label>
            </div>

            <label className="field">
              <span>Timezone</span>
              <input
                className="text-input"
                name="timezone"
                onChange={handleFieldChange("timezone")}
                required
                type="text"
                value={formState.timezone}
              />
            </label>

            <div className="entity-form-row">
              <label className="field">
                <span>Accounting standard</span>
                <input
                  className="text-input"
                  name="accountingStandard"
                  onChange={handleFieldChange("accountingStandard")}
                  placeholder="IFRS for SMEs"
                  type="text"
                  value={formState.accountingStandard}
                />
              </label>

              <label className="field">
                <span>Autonomy mode</span>
                <select
                  className="text-input"
                  name="autonomyMode"
                  onChange={handleFieldChange("autonomyMode")}
                  value={formState.autonomyMode}
                >
                  <option value="human_review">Human review</option>
                  <option value="reduced_interruption">Reduced interruption</option>
                </select>
              </label>
            </div>

            {errorMessage ? (
              <div className="status-banner danger" role="alert">
                {errorMessage}
              </div>
            ) : null}

            <button className="primary-button" disabled={isPending} type="submit">
              {isPending ? "Creating workspace..." : "Create workspace"}
            </button>
          </form>
        </SurfaceCard>

        <SurfaceCard title="Accessible Workspaces" subtitle="Your entities">
          {isLoading ? <p className="form-helper">Loading workspaces...</p> : null}
          {!isLoading && entities.length === 0 ? (
            <p className="form-helper">
              No workspaces exist yet. Create the first entity to start the close-run backbone.
            </p>
          ) : null}

          <div className="entity-card-list">
            {entities.map((entity) => (
              <Link className="entity-card-link" href={`/entities/${entity.id}`} key={entity.id}>
                <article className="entity-card">
                  <div className="entity-card-header">
                    <div>
                      <p className="eyebrow entity-card-eyebrow">
                        {entity.base_currency} workspace
                      </p>
                      <h2>{entity.name}</h2>
                    </div>
                    <span className="entity-status-chip">{entity.status.replace("_", " ")}</span>
                  </div>

                  <dl className="entity-meta-grid">
                    <div>
                      <dt>Default actor</dt>
                      <dd>{entity.default_actor?.full_name ?? "Unassigned"}</dd>
                    </div>
                    <div>
                      <dt>Members</dt>
                      <dd>{entity.member_count}</dd>
                    </div>
                    <div>
                      <dt>Timezone</dt>
                      <dd>{entity.timezone}</dd>
                    </div>
                    <div>
                      <dt>Language</dt>
                      <dd>{entity.workspace_language.toUpperCase()}</dd>
                    </div>
                  </dl>

                  <div className="entity-card-footer">
                    <div>
                      <strong>Last activity</strong>
                      <p>{entity.last_activity?.summary ?? "No activity has been recorded yet."}</p>
                    </div>
                    <span>
                      {formatDateTime(entity.last_activity?.created_at ?? entity.updated_at)}
                    </span>
                  </div>
                </article>
              </Link>
            ))}
          </div>
        </SurfaceCard>
      </section>
    </div>
  );
}

async function loadEntities(options: {
  onError: (message: string | null) => void;
  onLoaded: (entities: readonly EntitySummary[]) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const response = await listEntities();
    options.onLoaded(response.entities);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveEntityErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function emptyStringToUndefined(value: string): string | undefined {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : undefined;
}

function buildCreateEntityPayload(formState: Readonly<CreateEntityFormState>): CreateEntityRequest {
  const accountingStandard = emptyStringToUndefined(formState.accountingStandard);
  const legalName = emptyStringToUndefined(formState.legalName);

  return {
    autonomy_mode: formState.autonomyMode,
    base_currency: formState.baseCurrency,
    country_code: formState.countryCode,
    name: formState.name,
    timezone: formState.timezone,
    ...(accountingStandard ? { accounting_standard: accountingStandard } : {}),
    ...(legalName ? { legal_name: legalName } : {}),
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
