/*
Purpose: Render the protected desktop application shell for authenticated operators.
Scope: Cookie-gated workspace chrome, client-side session recovery, and logout access.
Dependencies: The shared session heartbeat and Quartz workspace shell components.
*/

import type { ReactElement, ReactNode } from "react";
import { SessionHeartbeat } from "../../components/auth/SessionHeartbeat";
import { QuartzWorkspaceShell } from "../../components/layout/QuartzWorkspaceShell";

type ProtectedAppLayoutProps = {
  children: ReactNode;
};

/**
 * Purpose: Wrap protected pages in the canonical desktop shell after middleware admits the request.
 * Inputs: The route segment content for authenticated workspace pages.
 * Outputs: A cookie-gated layout with client-side session validation and recovery.
 * Behavior: Keeps protected navigation independent from server-side auth roundtrips so route loads stay resilient.
 */
export default function ProtectedAppLayout({
  children,
}: Readonly<ProtectedAppLayoutProps>): ReactElement {
  return (
    <>
      <SessionHeartbeat />
      <QuartzWorkspaceShell>{children}</QuartzWorkspaceShell>
    </>
  );
}
