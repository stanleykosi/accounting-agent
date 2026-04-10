/*
Purpose: Render the protected desktop application shell for authenticated operators.
Scope: Session-aware chrome, operator identity display, long-session recovery, and logout access.
Dependencies: Next.js request headers, middleware-forwarded auth session data, and auth client components.
*/

import Link from "next/link";
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
    <div className="workspace-shell">
      <SessionHeartbeat />

      <header className="workspace-topbar">
        <div className="workspace-brand">
          <p className="eyebrow">Authenticated Workspace</p>
          <h1>Accounting AI Agent</h1>
          <p className="workspace-subtitle">
            Signed in as {session.user.full_name} for close-run review, approvals, and evidence
            tracing.
          </p>
        </div>

        <div className="workspace-actions">
          <nav aria-label="Primary workspace navigation" className="workspace-nav">
            <Link className="workspace-link" href={DEFAULT_WORKSPACE_PATH}>
              Workspace
            </Link>
          </nav>

          <div className="workspace-user-pill">
            <span className="workspace-user-initials">{operatorInitials}</span>
            <div>
              <strong>{session.user.full_name}</strong>
              <span>{session.user.email}</span>
            </div>
          </div>

          <LogoutButton />
        </div>
      </header>

      <main className="workspace-main">{children}</main>
    </div>
  );
}
