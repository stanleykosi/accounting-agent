/*
Purpose: Render the protected desktop application shell for authenticated operators.
Scope: Session-aware chrome, operator identity display, long-session recovery, and logout access.
Dependencies: Next.js request headers, middleware-forwarded auth session data, and auth client components.
*/

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactElement, ReactNode } from "react";
import { SessionHeartbeat } from "../../components/auth/SessionHeartbeat";
import { QuartzWorkspaceShell } from "../../components/layout/QuartzWorkspaceShell";
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
    <>
      <SessionHeartbeat />
      <QuartzWorkspaceShell
        userEmail={session.user.email}
        userFullName={session.user.full_name}
        userInitials={operatorInitials}
      >
        {children}
      </QuartzWorkspaceShell>
    </>
  );
}
