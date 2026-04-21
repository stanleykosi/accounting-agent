/*
Purpose: Render the dedicated assistant workbench for a close run inside the Quartz workspace shell.
Scope: Route validation plus full-page composition around the grounded ChatRail workspace.
Dependencies: Next.js routing primitives and the ChatRail component.
*/

import { notFound } from "next/navigation";
import { ChatRail } from "../../../../../../../components/chat/ChatRail";

interface ChatPageProps {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
}

export default async function ChatPage({ params }: Readonly<ChatPageProps>) {
  const { closeRunId, entityId } = await params;

  if (!entityId || !closeRunId) {
    notFound();
  }

  return (
    <div className="quartz-page quartz-chat-page">
      <header className="quartz-page-header">
        <div>
          <p className="quartz-kpi-label">Assistant</p>
          <h1>Assistant Workspace</h1>
          <p className="quartz-page-subtitle">
            Ask questions, upload source documents, and continue the close from one clean
            conversation workspace.
          </p>
        </div>
      </header>

      <section className="quartz-section">
        <div className="quartz-chat-workbench-shell">
          <ChatRail closeRunId={closeRunId} entityId={entityId} presentation="workspace" />
        </div>
      </section>
    </div>
  );
}
