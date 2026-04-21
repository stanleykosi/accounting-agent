"use client";

import { useRouter } from "next/navigation";
import {
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import {
  EntityApiError,
  createEntity,
  type CreateEntityRequest,
} from "../../../../lib/entities/api";

type WorkspaceSetupFormState = {
  baseCurrency: string;
  name: string;
};

const defaultFormState: WorkspaceSetupFormState = {
  baseCurrency: "NGN",
  name: "",
};

export default function WorkspaceCreationPage(): ReactElement {
  const router = useRouter();
  const [formState, setFormState] = useState<WorkspaceSetupFormState>(defaultFormState);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const handleFieldChange =
    (fieldName: keyof WorkspaceSetupFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      setFormState((currentState) => ({
        ...currentState,
        [fieldName]: event.target.value,
      }));
    };

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setErrorMessage(null);

    startTransition(() => {
      void createEntity(buildCreateEntityPayload(formState))
        .then((workspace) => {
          router.push(`/entities/${workspace.id}`);
          router.refresh();
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveEntityErrorMessage(error));
        });
    });
  };

  return (
    <main className="quartz-auth-shell">
      <section className="quartz-setup-card">
        <div className="quartz-setup-card-body">
          <header
            className="quartz-form-row"
            style={{ borderBottom: "1px solid var(--quartz-border)", paddingBottom: "16px" }}
          >
            <div className="quartz-setup-brand">
              <span>AA</span>
              <span
                style={{
                  color: "var(--quartz-muted)",
                  fontFamily: "var(--font-body)",
                  fontSize: "0.78rem",
                  fontWeight: 600,
                }}
              >
                Agent
              </span>
            </div>
            <span className="quartz-step-indicator">Step 01 / 01</span>
          </header>

          <div>
            <h1 className="quartz-setup-title">Initialize Your Workspace</h1>
            <p className="quartz-setup-copy">
              Define the core parameters for this entity&apos;s ledger. These settings establish the
              base structural data for your accounting close cycle.
            </p>
          </div>

          <form className="quartz-setup-form" onSubmit={handleSubmit}>
            <label className="quartz-form-label">
              <span>Workspace Name</span>
              <input
                className="text-input"
                name="name"
                onChange={handleFieldChange("name")}
                placeholder="e.g. Apex Meridian Nigeria Ltd"
                required
                type="text"
                value={formState.name}
              />
            </label>

            <label className="quartz-form-label">
              <span>Default Entity Currency</span>
              <select
                className="text-input"
                name="baseCurrency"
                onChange={handleFieldChange("baseCurrency")}
                value={formState.baseCurrency}
              >
                <option value="NGN">NGN - Nigerian Naira</option>
                <option value="USD">USD - US Dollar</option>
                <option value="EUR">EUR - Euro</option>
                <option value="GBP">GBP - British Pound</option>
              </select>
            </label>

            <p className="quartz-form-note">
              This currency will be used as the base for reporting and assistant-led anomaly
              detection. Country, timezone, and review routing can be refined later.
            </p>

            {errorMessage ? (
              <div className="status-banner danger" role="alert">
                {errorMessage}
              </div>
            ) : null}

            <div className="quartz-divider" />

            <div className="quartz-form-row">
              <button
                className="quartz-form-link"
                onClick={() => router.push("/entities")}
                type="button"
              >
                Cancel
              </button>
              <button className="primary-button" disabled={isPending} type="submit">
                {isPending ? "Creating workspace..." : "Create Workspace"}
              </button>
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}

function buildCreateEntityPayload(
  formState: Readonly<WorkspaceSetupFormState>,
): CreateEntityRequest {
  return {
    autonomy_mode: "human_review",
    base_currency: formState.baseCurrency,
    country_code: "NG",
    name: formState.name.trim(),
    timezone: "Africa/Lagos",
  };
}

function resolveEntityErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError) {
    return error.message;
  }

  return "The workspace could not be created. Reload the page and try again.";
}
