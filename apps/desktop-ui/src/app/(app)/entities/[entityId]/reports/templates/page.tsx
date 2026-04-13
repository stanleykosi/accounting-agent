/*
Purpose: Render the entity report-template management workspace.
Scope: Template listing, creation, activation, guardrail validation, and commentary
overview for report generation runs within an entity workspace.
Dependencies: React hooks, route params, shared SurfaceCard, and the reports API helper module.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import {
  use,
  useCallback,
  useEffect,
  useState,
  useTransition,
  type FormEvent,
  type ReactElement,
} from "react";
import {
  ReportApiError,
  activateReportTemplate,
  createReportTemplate,
  listReportTemplates,
  validateReportTemplateGuardrails,
  type CreateReportTemplateRequest,
  type GuardrailValidationResponse,
  type GuardrailViolation,
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

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">Report Templates</h1>
          <p className="mt-1 text-sm text-gray-500">
            Manage report templates with mandatory section guardrails. Templates must include all
            five required workflow sections.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreateForm((prev) => !prev)}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-500"
        >
          {showCreateForm ? "Cancel" : "New Template"}
        </button>
      </div>

      {/* Guardrail validation result */}
      {guardrailResult && (
        <div className="mb-4">
          <SurfaceCard title="Guardrail Validation">
            <div
              className={`rounded-md p-4 ${
                guardrailResult.is_valid ? "bg-green-50 text-green-800" : "bg-red-50 text-red-800"
              }`}
            >
              <p className="font-medium">
                {guardrailResult.is_valid
                  ? "Template passes all guardrail checks."
                  : "Template has guardrail violations:"}
              </p>
              {guardrailResult.violations.length > 0 && (
                <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
                  {guardrailResult.violations.map((v: GuardrailViolation, i: number) => (
                    <li key={i}>
                      {v.section_key && <span className="font-mono text-xs">{v.section_key}</span>}{" "}
                      {v.message}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </SurfaceCard>
        </div>
      )}

      {/* Create template form */}
      {showCreateForm && (
        <SurfaceCard title="Create Report Template">
          <form
            onSubmit={(event) => {
              handleCreateTemplate(event);
            }}
            className="space-y-4"
          >
            <div>
              <label htmlFor="template-name" className="block text-sm font-medium text-gray-700">
                Template name
              </label>
              <input
                id="template-name"
                type="text"
                required
                value={createForm.name}
                onChange={(e) => setCreateForm((prev) => ({ ...prev, name: e.target.value }))}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                placeholder="e.g. Monthly Management Pack"
              />
            </div>

            <div>
              <label htmlFor="template-desc" className="block text-sm font-medium text-gray-700">
                Description (optional)
              </label>
              <textarea
                id="template-desc"
                value={createForm.description}
                onChange={(e) =>
                  setCreateForm((prev) => ({
                    ...prev,
                    description: e.target.value,
                  }))
                }
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                rows={2}
                placeholder="Optional description of this template..."
              />
            </div>

            {/* Sections */}
            <div>
              <label className="block text-sm font-medium text-gray-700">Report sections</label>
              <p className="mt-1 text-xs text-gray-500">
                All five mandatory sections are included and cannot be removed.
              </p>
              <div className="mt-2 divide-y divide-gray-100 rounded-md border border-gray-200">
                {createForm.sections.map((section, index) => (
                  <div
                    key={section.section_key}
                    className="flex items-center justify-between px-3 py-2"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-sm font-medium text-gray-900">{index + 1}.</span>
                      <span className="text-sm text-gray-700">{section.label}</span>
                      <code className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-500">
                        {section.section_key}
                      </code>
                    </div>
                    <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700">
                      Required
                    </span>
                  </div>
                ))}
              </div>
            </div>

            <div className="flex items-center gap-2">
              <input
                id="activate-immediately"
                type="checkbox"
                checked={createForm.activateImmediately}
                onChange={(e) =>
                  setCreateForm((prev) => ({
                    ...prev,
                    activateImmediately: e.target.checked,
                  }))
                }
                className="rounded border-gray-300"
              />
              <label htmlFor="activate-immediately" className="text-sm text-gray-700">
                Activate this template immediately
              </label>
            </div>

            {createError && (
              <div className="rounded-md bg-red-50 p-3 text-sm text-red-800">{createError}</div>
            )}

            <div className="flex justify-end">
              <button
                type="submit"
                disabled={isPending || !createForm.name.trim()}
                className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-500 disabled:opacity-50"
              >
                {isPending ? "Creating..." : "Create Template"}
              </button>
            </div>
          </form>
        </SurfaceCard>
      )}

      {/* Template list */}
      <SurfaceCard title="Templates">
        {loading ? (
          <p className="py-8 text-center text-sm text-gray-500">Loading templates...</p>
        ) : error ? (
          <div className="rounded-md bg-red-50 p-4 text-sm text-red-800">{error}</div>
        ) : templates.length === 0 ? (
          <div className="py-8 text-center">
            <p className="text-sm text-gray-500">
              No report templates yet. Create one to get started.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-gray-100">
            {templates.map((template) => (
              <TemplateRow
                key={template.id}
                template={template}
                isActive={template.id === activeTemplateId}
                onActivate={() => {
                  handleActivateTemplate(template.id);
                }}
                onValidate={() => {
                  void handleValidateGuardrails(template.id);
                }}
                guardrailLoading={guardrailLoading}
              />
            ))}
          </div>
        )}
      </SurfaceCard>
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
    <div className="flex items-center justify-between px-4 py-3">
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-900">{template.name}</span>
          {isActive && (
            <span className="rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800">
              Active
            </span>
          )}
          <span className="text-xs text-gray-400">v{template.version_no}</span>
        </div>
        <div className="mt-1 flex items-center gap-4 text-xs text-gray-500">
          <span>{template.section_count} sections</span>
          <span>Source: {template.source}</span>
          {template.has_required_sections ? (
            <span className="text-green-600">All required sections present</span>
          ) : (
            <span className="text-red-600">Missing required sections</span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onValidate}
          disabled={guardrailLoading}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          {guardrailLoading ? "Checking..." : "Validate"}
        </button>
        {!isActive && (
          <button
            type="button"
            onClick={onActivate}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
          >
            Activate
          </button>
        )}
      </div>
    </div>
  );
}
