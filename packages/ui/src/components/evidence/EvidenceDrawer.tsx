/*
Purpose: Provide a shared evidence drawer surface for source-linked review context.
Scope: Drawer chrome, evidence reference listing, confidence labels, and empty-state rendering.
Dependencies: React rendering primitives plus the shared confidence badge for consistent review styling.
*/

import type { CSSProperties, ReactElement } from "react";
import { ConfidenceBadge } from "../review/ConfidenceBadge";

export type EvidenceDrawerReference = {
  confidence?: number | null;
  id: string;
  kind?: string;
  label: string;
  location: string;
  snippet?: string | null;
};

export type EvidenceDrawerProps = {
  emptyMessage?: string;
  isOpen: boolean;
  onClose: () => void;
  references: readonly EvidenceDrawerReference[];
  sourceLabel: string;
  title: string;
};

const drawerContainerStyle: CSSProperties = {
  background:
    "linear-gradient(180deg, rgba(250, 245, 239, 0.98) 0%, rgba(246, 239, 231, 0.92) 100%)",
  border: "1px solid rgba(52, 72, 63, 0.14)",
  borderRadius: "18px",
  boxShadow: "0 16px 42px rgba(22, 32, 25, 0.08)",
  display: "grid",
  gap: "14px",
  padding: "18px",
};

/**
 * Purpose: Render source-linked evidence snippets for the currently selected review target.
 * Inputs: Drawer visibility, source label, evidence references, and close callback.
 * Outputs: A compact evidence drawer suitable for review workspaces and side rails.
 * Behavior: Fails closed by rendering nothing when the drawer is not open.
 */
export function EvidenceDrawer({
  emptyMessage = "No evidence references are available for this selection.",
  isOpen,
  onClose,
  references,
  sourceLabel,
  title,
}: Readonly<EvidenceDrawerProps>): ReactElement | null {
  if (!isOpen) {
    return null;
  }

  return (
    <aside aria-label="Evidence drawer" style={drawerContainerStyle}>
      <header style={{ alignItems: "flex-start", display: "flex", justifyContent: "space-between", gap: "12px" }}>
        <div style={{ display: "grid", gap: "4px" }}>
          <p
            style={{
              color: "#4d5b52",
              fontSize: "0.78rem",
              fontWeight: 700,
              letterSpacing: "0.14em",
              margin: 0,
              textTransform: "uppercase",
            }}
          >
            {sourceLabel}
          </p>
          <h3 style={{ fontSize: "1.05rem", margin: 0 }}>{title}</h3>
        </div>
        <button
          aria-label="Close evidence drawer"
          onClick={onClose}
          style={{
            appearance: "none",
            background: "rgba(22, 32, 25, 0.06)",
            border: "1px solid rgba(52, 72, 63, 0.16)",
            borderRadius: "10px",
            color: "#162019",
            cursor: "pointer",
            font: "inherit",
            fontWeight: 700,
            minHeight: "32px",
            padding: "0 12px",
          }}
          type="button"
        >
          Close
        </button>
      </header>

      {references.length === 0 ? (
        <p style={{ color: "#4d5b52", margin: 0 }}>{emptyMessage}</p>
      ) : (
        <ul style={{ display: "grid", gap: "10px", listStyle: "none", margin: 0, padding: 0 }}>
          {references.map((reference) => (
            <li
              key={reference.id}
              style={{
                background: "rgba(255, 255, 255, 0.58)",
                border: "1px solid rgba(52, 72, 63, 0.1)",
                borderRadius: "12px",
                display: "grid",
                gap: "6px",
                padding: "12px",
              }}
            >
              <div style={{ alignItems: "center", display: "flex", flexWrap: "wrap", gap: "8px" }}>
                <strong style={{ fontSize: "0.92rem" }}>{reference.label}</strong>
                {reference.kind ? (
                  <span
                    style={{
                      background: "rgba(214, 230, 221, 0.72)",
                      borderRadius: "999px",
                      color: "#114f49",
                      fontSize: "0.74rem",
                      fontWeight: 700,
                      padding: "3px 8px",
                      textTransform: "capitalize",
                    }}
                  >
                    {reference.kind.replaceAll("_", " ")}
                  </span>
                ) : null}
                {typeof reference.confidence === "number" ? (
                  <ConfidenceBadge score={reference.confidence} size="compact" />
                ) : null}
              </div>
              <p style={{ color: "#4d5b52", margin: 0 }}>
                <strong style={{ color: "#2f3f35" }}>Location:</strong> {reference.location}
              </p>
              <p style={{ margin: 0 }}>{reference.snippet ?? "No snippet provided."}</p>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
