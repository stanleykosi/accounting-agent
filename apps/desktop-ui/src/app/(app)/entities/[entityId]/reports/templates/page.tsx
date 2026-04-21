/*
Purpose: Render the entity report-template management workspace.
Scope: Template listing, creation, activation, guardrail validation, and commentary
overview for report generation runs within an entity workspace.
Dependencies: React hooks, route params, Next links, and the reports API helper module.
*/

"use client";

import Link from "next/link";
import {
  use,
  useCallback,
  useEffect,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../../../../../../components/layout/QuartzIcons";
import {
  ReportApiError,
  activateReportTemplate,
  createReportTemplate,
  listReportTemplates,
  validateReportTemplateGuardrails,
  type CreateReportTemplateRequest,
  type GuardrailValidationResponse,
  type ReportSectionDefinition,
  type ReportTemplateSummary,
} from "../../../../../../lib/reports";

type ReportTemplatesPageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

type CreateTemplateFormState = {
  name: string;
  description: string;
  activateImmediately: boolean;
  sections: ReportSectionDefinition[];
};

const MANDATORY_SECTION_KEYS = [
  "profit_and_loss",
  "balance_sheet",
  "cash_flow",
  "budget_variance",
  "kpi_dashboard",
] as const;

const MANDATORY_SECTION_LABELS: Record<string, string> = {
  profit_and_loss: "Profit and Loss",
  balance_sheet: "Balance Sheet",
  cash_flow: "Cash Flow",
  budget_variance: "Budget Variance Analysis",
  kpi_dashboard: "KPI Dashboard",
};

const defaultCreateTemplateFormState: CreateTemplateFormState = {
  name: "",
  description: "",
  activateImmediately: true,
  sections: MANDATORY_SECTION_KEYS.map((key, index) => ({
    section_key: key,
    label: MANDATORY_SECTION_LABELS[key] ?? key,
    display_order: index,
    is_required: true,
    section_config: {},
  })),
};

/* ------------------------------------------------------------------ */
/* Page component                                                      */
/* ------------------------------------------------------------------ */

export default function ReportTemplatesPage({ params }: ReportTemplatesPageProps): ReactElement {
  const resolvedParams = use(params);
  const { entityId } = resolvedParams;

  const [templates, setTemplates] = useState<readonly ReportTemplateSummary[]>([]);
  const [activeTemplateId, setActiveTemplateId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createForm, setCreateForm] = useState<CreateTemplateFormState>(
    defaultCreateTemplateFormState,
  );
  const [createError, setCreateError] = useState<string | null>(null);

  const [guardrailResult, setGuardrailResult] = useState<GuardrailValidationResponse | null>(null);
  const [guardrailLoading, setGuardrailLoading] = useState(false);

  const loadTemplates = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listReportTemplates(entityId);
      setTemplates(response.templates);
      setActiveTemplateId(response.active_template_id ?? null);
    } catch (err: unknown) {
      setError(err instanceof ReportApiError ? err.message : "Failed to load report templates.");
    } finally {
      setLoading(false);
    }
  }, [entityId]);

  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  const handleCreateTemplate = (event: FormEvent) => {
    event.preventDefault();
    setCreateError(null);

    const payload: CreateReportTemplateRequest = {
      name: createForm.name.trim(),
      description: createForm.description.trim() || null,
      sections: createForm.sections,
      guardrail_config: {},
      activate_immediately: createForm.activateImmediately,
    };

    startTransition(() => {
      void createReportTemplate(entityId, payload)
        .then(async () => {
          setShowCreateForm(false);
          setCreateForm(defaultCreateTemplateFormState);
          await loadTemplates();
        })
        .catch((err: unknown) => {
          setCreateError(
            err instanceof ReportApiError ? err.message : "Failed to create report template.",
          );
        });
    });
  };

  const handleActivateTemplate = (templateId: string) => {
    startTransition(() => {
      void activateReportTemplate(entityId, templateId)
        .then(async () => {
          await loadTemplates();
        })
        .catch((err: unknown) => {
          setError(err instanceof ReportApiError ? err.message : "Failed to activate template.");
        });
    });
  };

  const handleValidateGuardrails = async (templateId: string) => {
    setGuardrailLoading(true);
    setGuardrailResult(null);
    try {
      const result = await validateReportTemplateGuardrails(entityId, templateId);
      setGuardrailResult(result);
    } catch {
      setGuardrailResult(null);
    } finally {
      setGuardrailLoading(false);
    }
  };

  const handleCreateFieldChange =
    (fieldName: "description" | "name") =>
    (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>): void => {
      const nextValue = event.target.value;
      setCreateForm((currentState) => ({
        ...currentState,
        [fieldName]: nextValue,
      }));
    };

  const handleActivateImmediatelyChange = (event: ChangeEvent<HTMLInputElement>): void => {
    setCreateForm((currentState) => ({
      ...currentState,
      activateImmediately: event.currentTarget.checked,
    }));
  };

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Report Templates</h1>
            <p className="quartz-page-subtitle">
              Manage governed report templates for this entity. Every template must include the
              five canonical workflow sections before it can become active.
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
            <button
              className="primary-button"
              onClick={() => setShowCreateForm((prev) => !prev)}
              type="button"
            >
              {showCreateForm ? "Close Template Form" : "New Template"}
            </button>
          </div>
        </header>

        {guardrailResult ? (
          <section className="quartz-section">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Guardrail Validation</p>
                  <h2 className="quartz-section-title">
                    {guardrailResult.is_valid
                      ? "Template passes all guardrails"
                      : "Template has guardrail violations"}
                  </h2>
                </div>
              </div>
              {guardrailResult.violations.length > 0 ? (
                <div className="quartz-reasoning-list">
                  {guardrailResult.violations.map((violation, index) => (
                    <div className="quartz-reasoning-item" key={`${violation.section_key ?? "global"}-${index}`}>
                      <strong>{violation.section_key ?? "Template rule"}</strong>
                      <span>{violation.message}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="status-banner success" role="status">
                  No violations found.
                </div>
              )}
            </article>
          </section>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid quartz-kpi-grid-triple">
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Template Count</p>
              <p className="quartz-kpi-value">{templates.length}</p>
              <p className="quartz-kpi-meta">Available report template versions</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Active Template</p>
              <p className="quartz-kpi-value quartz-kpi-value-small">
                {templates.find((template) => template.id === activeTemplateId)?.name ?? "None active"}
              </p>
              <p className="quartz-kpi-meta">Current reporting baseline</p>
            </article>
            <article className="quartz-kpi-tile highlight">
              <p className="quartz-kpi-label">Mandatory Sections</p>
              <p className="quartz-kpi-value">{MANDATORY_SECTION_KEYS.length}</p>
              <p className="quartz-kpi-meta">Required sections on every template</p>
            </article>
          </div>
        </section>

        {showCreateForm ? (
          <section className="quartz-section">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Creation</p>
                  <h2 className="quartz-section-title">Create Report Template</h2>
                </div>
              </div>
              <form className="quartz-settings-form" onSubmit={handleCreateTemplate}>
                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Template Name</span>
                    <input
                      className="text-input"
                      onChange={handleCreateFieldChange("name")}
                      placeholder="e.g. Monthly Management Pack"
                      required
                      type="text"
                      value={createForm.name}
                    />
                  </label>
                  <label className="quartz-settings-checkbox">
                    <input
                      checked={createForm.activateImmediately}
                      onChange={handleActivateImmediatelyChange}
                      type="checkbox"
                    />
                    <span>Activate immediately after creation</span>
                  </label>
                </div>

                <label className="quartz-form-label">
                  <span>Description</span>
                  <textarea
                    className="text-input quartz-compact-textarea"
                    onChange={handleCreateFieldChange("description")}
                    placeholder="Optional description of this template..."
                    value={createForm.description}
                  />
                </label>

                <article className="quartz-card soft quartz-settings-card">
                  <div className="quartz-section-header quartz-section-header-tight">
                    <div>
                      <p className="quartz-card-eyebrow">Sections</p>
                      <h3>Mandatory Template Blocks</h3>
                    </div>
                  </div>
                  <div className="quartz-summary-list">
                    {createForm.sections.map((section, index) => (
                      <div className="quartz-summary-row" key={section.section_key}>
                        <div>
                          <strong>
                            {index + 1}. {section.label}
                          </strong>
                          <div className="quartz-table-secondary">{section.section_key}</div>
                        </div>
                        <span className="quartz-status-badge success">Required</span>
                      </div>
                    ))}
                  </div>
                </article>

                {createError ? (
                  <div className="status-banner danger" role="alert">
                    {createError}
                  </div>
                ) : null}

                <div className="quartz-button-row">
                  <button
                    className="primary-button"
                    disabled={isPending || !createForm.name.trim()}
                    type="submit"
                  >
                    {isPending ? "Creating..." : "Create Template"}
                  </button>
                </div>
              </form>
            </article>
          </section>
        ) : null}

        <section className="quartz-section">
          <article className="quartz-card quartz-card-table-shell">
            <div className="quartz-section-header">
              <div>
                <h2 className="quartz-section-title">Templates</h2>
                <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                  Activate or validate the versions available for this entity.
                </p>
              </div>
            </div>
            {loading ? (
              <div className="quartz-empty-state">Loading report templates...</div>
            ) : error ? (
              <div className="status-banner danger" role="alert">
                {error}
              </div>
            ) : templates.length === 0 ? (
              <div className="quartz-empty-state">
                No report templates exist yet. Create one to establish the reporting baseline.
              </div>
            ) : (
              <table className="quartz-table">
                <thead>
                  <tr>
                    <th>Template</th>
                    <th>Coverage</th>
                    <th>Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {templates.map((template) => (
                    <TemplateRow
                      guardrailLoading={guardrailLoading}
                      isActive={template.id === activeTemplateId}
                      key={template.id}
                      onActivate={() => {
                        handleActivateTemplate(template.id);
                      }}
                      onValidate={() => {
                        void handleValidateGuardrails(template.id);
                      }}
                      template={template}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </article>
        </section>
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Template row sub-component                                          */
/* ------------------------------------------------------------------ */

type TemplateRowProps = {
  template: ReportTemplateSummary;
  isActive: boolean;
  onActivate: () => void;
  onValidate: () => void;
  guardrailLoading: boolean;
};

function TemplateRow({
  template,
  isActive,
  onActivate,
  onValidate,
  guardrailLoading,
}: TemplateRowProps): ReactElement {
  return (
    <tr>
      <td>
        <div className="quartz-table-primary">
          {template.name} <span className="quartz-table-secondary">v{template.version_no}</span>
        </div>
        <div className="quartz-table-secondary">Source: {template.source}</div>
      </td>
      <td>
        <div className="quartz-table-primary">{template.section_count} sections</div>
        <div className="quartz-table-secondary">
          {template.has_required_sections
            ? "All required sections present"
            : "Missing required sections"}
        </div>
      </td>
      <td>
        <span className={`quartz-status-badge ${isActive ? "success" : "neutral"}`}>
          {isActive ? "Active" : "Inactive"}
        </span>
      </td>
      <td className="quartz-table-center">
        <div className="quartz-inline-actions">
          <button
            className="secondary-button quartz-inline-button"
            disabled={guardrailLoading}
            onClick={onValidate}
            type="button"
          >
            {guardrailLoading ? "Checking..." : "Validate"}
          </button>
          {!isActive ? (
            <button className="primary-button quartz-inline-button" onClick={onActivate} type="button">
              Activate
            </button>
          ) : null}
        </div>
      </td>
    </tr>
  );
}
