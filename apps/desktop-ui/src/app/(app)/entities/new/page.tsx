"use client";

import {
  autonomyModeDefinitions,
  autonomyModeOrder,
  type AutonomyMode,
} from "@accounting-ai-agent/ui";
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
  accountingStandard: string;
  autonomyMode: AutonomyMode;
  baseCurrency: string;
  countryCode: string;
  legalName: string;
  name: string;
  timezone: string;
};

const defaultFormState: WorkspaceSetupFormState = {
  accountingStandard: "",
  autonomyMode: "human_review",
  baseCurrency: "NGN",
  countryCode: "NG",
  legalName: "",
  name: "",
  timezone: "Africa/Lagos",
};

const countryOptions = [
  { code: "NG", label: "Nigeria", timezone: "Africa/Lagos" },
  { code: "GH", label: "Ghana", timezone: "Africa/Accra" },
  { code: "KE", label: "Kenya", timezone: "Africa/Nairobi" },
  { code: "ZA", label: "South Africa", timezone: "Africa/Johannesburg" },
  { code: "AE", label: "United Arab Emirates", timezone: "Asia/Dubai" },
  { code: "GB", label: "United Kingdom", timezone: "Europe/London" },
  { code: "US", label: "United States", timezone: "America/New_York" },
] as const;

const timezoneOptions = [
  "Africa/Lagos",
  "Africa/Accra",
  "Africa/Nairobi",
  "Africa/Johannesburg",
  "Asia/Dubai",
  "Europe/London",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
] as const;

const accountingStandardOptions = [
  "IFRS",
  "US GAAP",
  "UK GAAP",
  "IPSAS",
  "Local GAAP",
  "Other",
] as const;

const autonomyModeOptions = autonomyModeOrder as readonly AutonomyMode[];

export default function WorkspaceCreationPage(): ReactElement {
  const router = useRouter();
  const [formState, setFormState] = useState<WorkspaceSetupFormState>(defaultFormState);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const handleFieldChange =
    (fieldName: keyof WorkspaceSetupFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void => {
      const nextValue = event.target.value;
      setFormState((currentState) => ({
        ...currentState,
        [fieldName]:
          fieldName === "countryCode"
            ? nextValue
            : nextValue,
        ...(fieldName === "countryCode"
          ? {
              timezone:
                countryOptions.find((option) => option.code === nextValue)?.timezone ??
                currentState.timezone,
            }
          : {}),
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
          <div>
            <h1 className="quartz-setup-title">Create Workspace</h1>
            <p className="quartz-setup-copy">
              Set the core entity defaults for this workspace so the close flow starts in the right
              operating posture.
            </p>
          </div>

          <form className="quartz-setup-form" onSubmit={handleSubmit}>
            <div className="quartz-form-grid">
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
                <span>Legal Entity Name</span>
                <input
                  className="text-input"
                  name="legalName"
                  onChange={handleFieldChange("legalName")}
                  placeholder="Optional legal registration name"
                  type="text"
                  value={formState.legalName}
                />
              </label>
            </div>

            <div className="quartz-form-grid">
              <label className="quartz-form-label">
                <span>Default Currency</span>
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
                  <option value="KES">KES - Kenyan Shilling</option>
                  <option value="ZAR">ZAR - South African Rand</option>
                  <option value="AED">AED - UAE Dirham</option>
                </select>
              </label>

              <label className="quartz-form-label">
                <span>Accounting Standard</span>
                <select
                  className="text-input"
                  name="accountingStandard"
                  onChange={handleFieldChange("accountingStandard")}
                  value={formState.accountingStandard}
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
                  name="countryCode"
                  onChange={handleFieldChange("countryCode")}
                  value={formState.countryCode}
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
                  name="timezone"
                  onChange={handleFieldChange("timezone")}
                  value={formState.timezone}
                >
                  {timezoneOptions.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <section className="quartz-setup-section">
              <div className="quartz-setup-section-header">
                <h2 className="quartz-setup-section-title">Review Routing</h2>
                <p className="quartz-setup-section-copy">
                  Choose how the workspace routes accounting actions and approvals.
                </p>
              </div>
              <div className="quartz-choice-grid">
                {autonomyModeOptions.map((mode) => {
                  const definition = autonomyModeDefinitions.find(
                    (option) => option.code === mode,
                  );
                  const isSelected = formState.autonomyMode === mode;
                  if (definition === undefined) {
                    return null;
                  }

                  return (
                    <label
                      className={`quartz-choice-card ${isSelected ? "selected" : ""}`}
                      key={mode}
                    >
                      <input
                        checked={isSelected}
                        name="autonomyMode"
                        onChange={handleFieldChange("autonomyMode")}
                        type="radio"
                        value={mode}
                      />
                      <div className="quartz-choice-card-content">
                        <div className="quartz-choice-card-header">
                          <strong>{definition.label}</strong>
                          {mode === "human_review" ? (
                            <span className="quartz-status-badge success">Default</span>
                          ) : null}
                        </div>
                        <p>{definition.description}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
            </section>

            {errorMessage ? (
              <div className="status-banner danger" role="alert">
                {errorMessage}
              </div>
            ) : null}

            <div className="quartz-divider" />

            <div className="quartz-form-row quartz-setup-actions">
              <button
                className="secondary-button"
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
    accounting_standard: emptyStringToNull(formState.accountingStandard),
    autonomy_mode: formState.autonomyMode,
    base_currency: formState.baseCurrency,
    country_code: formState.countryCode,
    legal_name: emptyStringToNull(formState.legalName),
    name: formState.name.trim(),
    timezone: formState.timezone,
  };
}

function emptyStringToNull(value: string): string | null {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function resolveEntityErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError) {
    return error.message;
  }

  return "The workspace could not be created. Reload the page and try again.";
}
