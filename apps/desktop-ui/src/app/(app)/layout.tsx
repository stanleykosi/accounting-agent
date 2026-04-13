/*
Purpose: Render the protected desktop application shell for authenticated operators.
Scope: Session-aware chrome, operator identity display, long-session recovery, and logout access.
Dependencies: Next.js request headers, middleware-forwarded auth session data, and auth client components.
*/

import { AppShell, CommandPalette } from "@accounting-ai-agent/ui";
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactElement, ReactNode } from "react";
import { LogoutButton } from "../../components/auth/LogoutButton";
import { SessionHeartbeat } from "../../components/auth/SessionHeartbeat";
import {
  AUTH_SESSION_HEADER_NAME,
  DEFAULT_WORKSPACE_PATH,
  buildLoginRedirectPath,
  deserializeSessionHeader,
  getOperatorInitials,
} from "../../lib/auth/session";

type ProtectedAppLayoutProps = {
  children: ReactNode;
};

const commandPaletteItems = [
  {
    description:
      "Open the global dashboard for review pressure, close-run status, and recent activity.",
    href: "/",
    id: "dashboard",
    keywords: ["home", "overview", "status"],
    label: "Global dashboard",
  },
  {
    description:
      "Open the entity directory to manage workspaces, memberships, and period close runs.",
    href: "/entities",
    id: "entities",
    keywords: ["workspace", "directory", "companies"],
    label: "Entity workspaces",
  },
] as const;

/**
 * Purpose: Wrap protected pages in the canonical authenticated desktop shell.
 * Inputs: The route segment content for authenticated workspace pages.
 * Outputs: A session-aware layout with operator identity, navigation, and logout controls.
 * Behavior: Fails closed by redirecting to login when middleware did not provide a validated session header.
 */
export default async function ProtectedAppLayout({
  children,
}: Readonly<ProtectedAppLayoutProps>): Promise<ReactElement> {
  const requestHeaders = await headers();
  const session = deserializeSessionHeader(requestHeaders.get(AUTH_SESSION_HEADER_NAME));
  if (session === null) {
    redirect(
      buildLoginRedirectPath({
        nextPath: DEFAULT_WORKSPACE_PATH,
        reason: "auth-required",
      }),
    );
  }

  const operatorInitials = getOperatorInitials(session);

  return (
    <>
      <SessionHeartbeat />
      <AppShell
        brandEyebrow="Authenticated Workspace"
        brandSubtitle={`Signed in as ${session.user.full_name} for close-run review, approvals, and evidence tracing.`}
        brandTitle="Accounting AI Agent"
        commandPalette={<CommandPalette items={commandPaletteItems} />}
        navigationItems={[
          { href: DEFAULT_WORKSPACE_PATH, label: "Dashboard" },
          { href: "/entities", label: "Entities" },
        ]}
        userPanel={
          <div className="workspace-user-pill">
            <span className="workspace-user-initials">{operatorInitials}</span>
            <div>
              <strong>{session.user.full_name}</strong>
              <span>{session.user.email}</span>
            </div>
          </div>
        }
        utilityPanel={<LogoutButton />}
      >
        {children}
      </AppShell>
    </>
  );
}
