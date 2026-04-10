/*
Purpose: Provide a shared panel primitive for dense desktop finance layouts.
Scope: Reusable card surface with a small header and opinionated visual treatment.
Dependencies: React and the shared token language established in this package.
*/

import type { CSSProperties, ReactElement, ReactNode } from "react";

export type SurfaceTone = "accent" | "default";

export type SurfaceCardProps = {
  children: ReactNode;
  subtitle?: string;
  title: string;
  tone?: SurfaceTone;
};

const baseSurfaceStyle: CSSProperties = {
  background:
    "linear-gradient(180deg, rgba(255, 251, 245, 0.98) 0%, rgba(250, 244, 236, 0.9) 100%)",
  border: "1px solid rgba(52, 72, 63, 0.14)",
  borderRadius: "24px",
  boxShadow: "0 20px 56px rgba(22, 32, 25, 0.08)",
  padding: "24px",
};

const accentSurfaceStyle: CSSProperties = {
  background:
    "linear-gradient(160deg, rgba(214, 230, 221, 0.92) 0%, rgba(255, 243, 229, 0.98) 100%)",
};

/**
 * Purpose: Render a shared surface card with consistent spacing and typography.
 * Inputs: Title, optional subtitle, child content, and an optional visual tone.
 * Outputs: A React element that can be used in server or client components.
 * Behavior: Applies inline styles so the package remains zero-config for the first workspace step.
 */
export function SurfaceCard({
  children,
  subtitle,
  title,
  tone = "default",
}: Readonly<SurfaceCardProps>): ReactElement {
  const surfaceStyle =
    tone === "accent" ? { ...baseSurfaceStyle, ...accentSurfaceStyle } : baseSurfaceStyle;

  return (
    <section style={surfaceStyle}>
      <div style={{ display: "grid", gap: "6px", marginBottom: "16px" }}>
        <p
          style={{
            color: "#4d5b52",
            fontSize: "0.82rem",
            fontWeight: 700,
            letterSpacing: "0.14em",
            margin: 0,
            textTransform: "uppercase",
          }}
        >
          {subtitle ?? "Shared UI Primitive"}
        </p>
        <h2
          style={{
            fontSize: "1.45rem",
            letterSpacing: "-0.04em",
            lineHeight: 1.1,
            margin: 0,
          }}
        >
          {title}
        </h2>
      </div>
      {children}
    </section>
  );
}
