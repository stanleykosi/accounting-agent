/*
Purpose: Render a canonical confidence badge for evidence-backed review surfaces.
Scope: Shared confidence formatting, tone derivation, and compact badge presentation.
Dependencies: React plus the shared review tokens defined in packages/ui/src/styles/tokens.ts.
*/

import type { CSSProperties, ReactElement } from "react";
import {
  confidencePalette,
  type ConfidenceTone,
  reviewSurfacePalette,
  reviewTypography,
} from "../../styles/tokens";

export type ConfidenceBadgeSize = "compact" | "default";

export type ConfidenceBadgeProps = {
  label?: string;
  score: number | null;
  size?: ConfidenceBadgeSize;
  tone?: ConfidenceTone;
};

const sizeStyles: Readonly<Record<ConfidenceBadgeSize, CSSProperties>> = {
  compact: {
    fontSize: "0.74rem",
    minHeight: "24px",
    padding: "0 8px",
  },
  default: {
    fontSize: "0.82rem",
    minHeight: "30px",
    padding: "0 10px",
  },
};

/**
 * Purpose: Render a reusable confidence indicator with deterministic tone and labeling.
 * Inputs: Numeric score, optional explicit tone override, optional label, and badge density.
 * Outputs: A React span element suitable for dense review rows and evidence cards.
 * Behavior: Falls back to an explicit unknown state when no score is available.
 */
export function ConfidenceBadge({
  label,
  score,
  size = "default",
  tone,
}: Readonly<ConfidenceBadgeProps>): ReactElement {
  const resolvedTone = tone ?? deriveConfidenceTone(score);
  const resolvedLabel = label ?? formatConfidenceBadgeLabel(score);
  const palette = confidencePalette[resolvedTone];

  return (
    <span
      aria-label={`Confidence ${resolvedLabel}`}
      className={`ui-confidence-badge ui-confidence-badge-${resolvedTone} ui-confidence-badge-${size}`}
      data-tone={resolvedTone}
      style={{
        alignItems: "center",
        background: palette.background,
        border: `1px solid ${palette.border}`,
        borderRadius: "999px",
        color: palette.foreground,
        display: "inline-flex",
        fontFamily: reviewTypography.body,
        fontWeight: 700,
        gap: "6px",
        letterSpacing: "-0.01em",
        whiteSpace: "nowrap",
        ...sizeStyles[size],
      }}
      title={score === null ? "Confidence score unavailable." : `Confidence ${resolvedLabel}`}
    >
      <span
        aria-hidden="true"
        style={{
          background: palette.foreground,
          borderRadius: "999px",
          boxShadow: `0 0 0 3px ${palette.background}`,
          height: size === "compact" ? "7px" : "8px",
          width: size === "compact" ? "7px" : "8px",
        }}
      />
      <span style={{ color: reviewSurfacePalette.text }}>{resolvedLabel}</span>
    </span>
  );
}

/**
 * Purpose: Derive the canonical confidence tone from an optional bounded score.
 * Inputs: Optional confidence score expressed between 0 and 1.
 * Outputs: One of the four canonical confidence tones used throughout review surfaces.
 * Behavior: Treats scores below 0.75 as low confidence and missing scores as unknown.
 */
export function deriveConfidenceTone(score: number | null): ConfidenceTone {
  if (score === null) {
    return "unknown";
  }

  if (score >= 0.9) {
    return "high";
  }

  if (score >= 0.75) {
    return "medium";
  }

  return "low";
}

/**
 * Purpose: Format a confidence score into the compact label shown in badges and tables.
 * Inputs: Optional confidence score expressed between 0 and 1.
 * Outputs: Rounded percentage text or an explicit unknown label.
 * Behavior: Rounds to whole percentages to preserve dense table layout.
 */
export function formatConfidenceBadgeLabel(score: number | null): string {
  if (score === null) {
    return "Unknown";
  }

  return `${Math.round(score * 100)}%`;
}
