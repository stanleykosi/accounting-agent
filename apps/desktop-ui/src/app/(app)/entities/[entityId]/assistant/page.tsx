/*
Purpose: Render the entity-scoped assistant workspace for one accounting workspace.
Scope: Route validation plus full-page composition around the grounded ChatRail workspace.
Dependencies: Next.js routing primitives and the ChatRail component.
*/

import { notFound } from "next/navigation";
import { ChatRail } from "../../../../../components/chat/ChatRail";

interface EntityAssistantPageProps {
  params: Promise<{
    entityId: string;
  }>;
}

export default async function EntityAssistantPage({ params }: Readonly<EntityAssistantPageProps>) {
  const { entityId } = await params;

  if (!entityId) {
    notFound();
  }

  return (
    <div className="quartz-page quartz-chat-page">
      <div className="quartz-chat-workbench-shell">
        <ChatRail entityId={entityId} presentation="workspace" />
      </div>
    </div>
  );
}
