/*
Purpose: Surface the registered agent tool catalog and schema coverage outside
the chat workbench so operators can inspect capabilities across workflow pages.
Scope: Fetches the MCP tool manifest, summarizes schema coverage, and renders
compact capability previews with links back into the grounded agent workspace.
Dependencies: React, same-origin chat manifest helpers, and Next.js links.
*/

"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type CSSProperties, type ReactElement } from "react";
import {
  ChatApiError,
  extractToolSchemaFields,
  getChatToolManifest,
  summarizeToolSchema,
  type ChatToolManifest,
} from "../../lib/chat";

export type AgentCapabilityCatalogProps = {
  maxTools?: number;
  workbenchHref?: string;
};

export function AgentCapabilityCatalog({
  maxTools = 6,
  workbenchHref,
}: Readonly<AgentCapabilityCatalogProps>): ReactElement {
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [manifest, setManifest] = useState<ChatToolManifest | null>(null);

  useEffect(() => {
    void loadManifest({
      onError: setErrorMessage,
      onLoaded: setManifest,
      onLoadingChange: setIsLoading,
    });
  }, []);

  const visibleTools = useMemo(
    () => (manifest === null ? [] : manifest.tools.slice(0, maxTools)),
    [manifest, maxTools],
  );
  const manifestSummary = useMemo(() => {
    if (manifest === null) {
      return {
        schemaFieldCount: 0,
        structuredToolCount: 0,
        toolCount: 0,
      };
    }

    return manifest.tools.reduce(
      (summary, tool) => {
        const schemaSummary = summarizeToolSchema(tool.inputSchema);
        return {
          schemaFieldCount: summary.schemaFieldCount + schemaSummary.fieldCount,
          structuredToolCount:
            summary.structuredToolCount + (schemaSummary.fieldCount > 0 ? 1 : 0),
          toolCount: summary.toolCount + 1,
        };
      },
      {
        schemaFieldCount: 0,
        structuredToolCount: 0,
        toolCount: 0,
      },
    );
  }, [manifest]);

  if (isLoading) {
    return (
      <p className="form-helper">
        Loading the agent capability catalog and structured tool contracts...
      </p>
    );
  }

  if (manifest === null) {
    return (
      <div style={catalogStateStyle}>
        <p className="form-helper">
          {errorMessage ?? "The agent capability catalog could not be loaded."}
        </p>
      </div>
    );
  }

  return (
    <div style={catalogLayoutStyle}>
      <div style={catalogMetricGridStyle}>
        <CapabilityMetric label="Registered tools" value={String(manifestSummary.toolCount)} />
        <CapabilityMetric label="Structured tools" value={String(manifestSummary.structuredToolCount)} />
        <CapabilityMetric label="Schema fields" value={String(manifestSummary.schemaFieldCount)} />
        <CapabilityMetric label="Protocol" value={manifest.version} />
      </div>

      <p className="form-helper">
        The same deterministic tool catalog powers the operator workbench and external runtime
        surfaces. Schema-backed inputs make tool execution auditable and predictable.
      </p>

      <div style={toolListStyle}>
        {visibleTools.map((tool) => {
          const schemaSummary = summarizeToolSchema(tool.inputSchema);
          const schemaFields = extractToolSchemaFields(tool.inputSchema).slice(0, 4);
          return (
            <article className="dashboard-row" key={tool.name}>
              <div style={toolHeaderStyle}>
                <div>
                  <strong className="close-run-row-title">{tool.name}</strong>
                  <p className="close-run-row-meta">
                    {schemaSummary.fieldCount === 0
                      ? "No input fields"
                      : `${schemaSummary.fieldCount} fields • ${schemaSummary.requiredCount} required`}
                  </p>
                </div>
                <span className="entity-status-chip">
                  {schemaSummary.fieldCount === 0 ? "stateless" : "schema-backed"}
                </span>
              </div>
              <p className="form-helper">{tool.description}</p>
              {schemaFields.length > 0 ? (
                <div style={fieldChipRowStyle}>
                  {schemaFields.map((field) => (
                    <span key={`${tool.name}-${field.name}`} style={fieldChipStyle(field.required)}>
                      {field.name}
                      {field.required ? " *" : ""}
                    </span>
                  ))}
                </div>
              ) : null}
            </article>
          );
        })}
      </div>

      {workbenchHref ? (
        <div className="close-run-link-row">
          <Link className="workspace-link-inline" href={workbenchHref}>
            Open agent workbench
          </Link>
        </div>
      ) : null}
    </div>
  );
}

async function loadManifest(options: {
  onError: (message: string | null) => void;
  onLoaded: (value: ChatToolManifest) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const manifest = await getChatToolManifest();
    options.onLoaded(manifest);
    options.onError(null);
  } catch (error: unknown) {
    const message =
      error instanceof ChatApiError
        ? error.message
        : "The agent capability catalog could not be loaded.";
    options.onError(message);
  } finally {
    options.onLoadingChange(false);
  }
}

function CapabilityMetric({
  label,
  value,
}: Readonly<{
  label: string;
  value: string;
}>): ReactElement {
  return (
    <article style={metricCardStyle}>
      <span style={metricLabelStyle}>{label}</span>
      <strong style={metricValueStyle}>{value}</strong>
    </article>
  );
}

function fieldChipStyle(required: boolean): CSSProperties {
  return {
    background: required ? "rgba(76, 139, 245, 0.12)" : "rgba(183, 195, 214, 0.12)",
    border: `1px solid ${required ? "rgba(76, 139, 245, 0.28)" : "rgba(183, 195, 214, 0.18)"}`,
    borderRadius: 999,
    color: required ? "#4C8BF5" : "#B7C3D6",
    fontSize: 11,
    fontWeight: 600,
    padding: "4px 10px",
  };
}

const catalogLayoutStyle: CSSProperties = {
  display: "grid",
  gap: 16,
};

const catalogMetricGridStyle: CSSProperties = {
  display: "grid",
  gap: 12,
  gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
};

const catalogStateStyle: CSSProperties = {
  display: "grid",
  gap: 8,
};

const metricCardStyle: CSSProperties = {
  background: "rgba(14, 23, 38, 0.6)",
  border: "1px solid rgba(36, 50, 74, 0.9)",
  borderRadius: 14,
  display: "grid",
  gap: 6,
  padding: "14px 16px",
};

const metricLabelStyle: CSSProperties = {
  color: "#94A4BD",
  fontSize: 12,
  fontWeight: 500,
};

const metricValueStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 18,
  fontWeight: 700,
};

const toolListStyle: CSSProperties = {
  display: "grid",
  gap: 12,
};

const toolHeaderStyle: CSSProperties = {
  alignItems: "flex-start",
  display: "flex",
  gap: 12,
  justifyContent: "space-between",
};

const fieldChipRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  marginTop: 8,
};
