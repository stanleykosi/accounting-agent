/*
Purpose: Provide a reusable auth-focused frame for login, registration, and session recovery views.
Scope: Shared enterprise-styled auth container with optional notice messaging and supporting content.
Dependencies: React and inline design tokens so consumers do not need extra CSS configuration.
*/

import type { CSSProperties, ReactElement, ReactNode } from "react";

export type AuthGateTone = "default" | "warning";

export type AuthGateProps = {
  children: ReactNode;
  description: string;
  eyebrow?: string;
  notice?: string;
  noticeTone?: AuthGateTone;
  supportingContent?: ReactNode;
  title: string;
};

const panelStyle: CSSProperties = {
  backdropFilter: "blur(18px)",
  background:
    "linear-gradient(180deg, rgba(255, 251, 245, 0.98) 0%, rgba(248, 242, 234, 0.94) 100%)",
  border: "1px solid rgba(52, 72, 63, 0.14)",
  borderRadius: "30px",
  boxShadow: "0 24px 70px rgba(22, 32, 25, 0.12)",
  display: "grid",
  gap: "22px",
  padding: "30px",
};

const warningNoticeStyle: CSSProperties = {
  background: "rgba(185, 28, 28, 0.08)",
  border: "1px solid rgba(185, 28, 28, 0.16)",
  color: "#8f1d1d",
};

const defaultNoticeStyle: CSSProperties = {
  background: "rgba(15, 118, 110, 0.08)",
  border: "1px solid rgba(15, 118, 110, 0.14)",
  color: "#0f5e57",
};

/**
 * Purpose: Wrap auth-facing content in a consistent enterprise-style shell.
 * Inputs: Headline copy, an explanatory description, optional operator notice, and form content.
 * Outputs: A rendered auth frame suitable for server or client page composition.
 * Behavior: Keeps auth views visually aligned across login, registration, and session-recovery flows.
 */
export function AuthGate({
  children,
  description,
  eyebrow = "Local Operator Access",
  notice,
  noticeTone = "default",
  supportingContent,
  title,
}: Readonly<AuthGateProps>): ReactElement {
  const noticeStyle = noticeTone === "warning" ? warningNoticeStyle : defaultNoticeStyle;

  return (
    <section style={panelStyle}>
      <header style={{ display: "grid", gap: "10px" }}>
        <p
          style={{
            color: "#4d5b52",
            fontSize: "0.82rem",
            fontWeight: 700,
            letterSpacing: "0.16em",
            margin: 0,
            textTransform: "uppercase",
          }}
        >
          {eyebrow}
        </p>
        <h1
          style={{
            fontSize: "clamp(2.2rem, 4vw, 3.6rem)",
            letterSpacing: "-0.06em",
            lineHeight: 0.96,
            margin: 0,
            maxWidth: "12ch",
          }}
        >
          {title}
        </h1>
        <p
          style={{
            color: "#4d5b52",
            lineHeight: 1.7,
            margin: 0,
            maxWidth: "62ch",
          }}
        >
          {description}
        </p>
      </header>

      {notice ? (
        <div
          role="status"
          style={{
            ...noticeStyle,
            borderRadius: "18px",
            padding: "14px 16px",
          }}
        >
          {notice}
        </div>
      ) : null}

      <div style={{ display: "grid", gap: "22px", gridTemplateColumns: "minmax(0, 1fr)" }}>
        {children}
        {supportingContent ? (
          <aside
            style={{
              color: "#4d5b52",
              display: "grid",
              gap: "12px",
              lineHeight: 1.65,
            }}
          >
            {supportingContent}
          </aside>
        ) : null}
      </div>
    </section>
  );
}
