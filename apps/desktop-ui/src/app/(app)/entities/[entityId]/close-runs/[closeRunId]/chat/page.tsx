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
      <div className="quartz-chat-workbench-shell">
        <ChatRail closeRunId={closeRunId} entityId={entityId} presentation="workspace" />
      </div>
    </div>
  );
}
