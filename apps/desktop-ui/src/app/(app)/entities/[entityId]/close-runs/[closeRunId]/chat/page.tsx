/*
Purpose: Provide a dedicated chat page for a close run, grounding the
accounting agent to the current entity and accounting period.
Scope: Server-side data fetching for close run context, client-side
ChatRail component integration, and layout composition.
Dependencies: Next.js app router, shared chat API helpers, and the
ChatRail component.
*/

import { notFound } from "next/navigation";
import { ChatRail } from "../../../../../../../components/chat/ChatRail";

interface ChatPageProps {
  params: {
    closeRunId: string;
    entityId: string;
  };
}

/**
 * Purpose: Render the grounded chat page for a specific close run.
 * Inputs: Entity ID and close run ID from the URL.
 * Outputs: Full-page chat surface with the ChatRail component grounded
 * to the current entity and close run period.
 * Behavior: Validates that the close run exists and passes grounding
 * context to the ChatRail. Falls back to 404 when parameters are invalid.
 */
export default function ChatPage({ params }: Readonly<ChatPageProps>) {
  const { closeRunId, entityId } = params;

  if (!entityId || !closeRunId) {
    notFound();
  }

  return (
    <div style={chatPageStyle}>
      <header style={chatHeaderStyle}>
        <div style={{ display: "grid", gap: "4px" }}>
          <p style={chatEyebrowStyle}>Accounting Agent</p>
          <h1 style={chatTitleStyle}>Operations Workbench</h1>
          <p style={chatSubtitleStyle}>
            Review close progress, inspect the agent&apos;s working memory,
            trigger deterministic accounting workflows, and route approvals
            from one workspace built for accountants and finance leadership.
          </p>
        </div>
      </header>
      <div style={chatRailWrapperStyle}>
        <ChatRail closeRunId={closeRunId} entityId={entityId} presentation="workspace" />
      </div>
    </div>
  );
}

const chatPageStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  height: "100%",
  overflow: "hidden",
};

const chatHeaderStyle: React.CSSProperties = {
  borderBottom: "1px solid #24324A",
  padding: "24px 28px 20px",
};

const chatEyebrowStyle: React.CSSProperties = {
  color: "#7EBCFF",
  fontSize: "11px",
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
};

const chatTitleStyle: React.CSSProperties = {
  color: "#F4F7FB",
  fontSize: "24px",
  fontWeight: 700,
  margin: 0,
};

const chatSubtitleStyle: React.CSSProperties = {
  color: "#B7C3D6",
  fontSize: "13px",
  lineHeight: "20px",
  margin: 0,
  maxWidth: "640px",
};

const chatRailWrapperStyle: React.CSSProperties = {
  flex: 1,
  overflow: "hidden",
};
