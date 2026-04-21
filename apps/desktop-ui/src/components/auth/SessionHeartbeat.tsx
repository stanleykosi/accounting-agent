/*
Purpose: Keep long-lived protected desktop sessions fresh and recover cleanly when they expire.
Scope: Periodic session refresh, expiry redirects, and operator-facing refresh failure warnings.
Dependencies: React, Next.js navigation, and the same-origin auth proxy client helpers.
*/

"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useEffectEvent, useState, type ReactElement } from "react";
import { isSessionAuthError, readCurrentSession } from "../../lib/auth/client";
import { buildLoginRedirectPath } from "../../lib/auth/session";

const HEARTBEAT_INTERVAL_MS = 5 * 60 * 1_000;

/**
 * Purpose: Refresh the active auth session during long-running review work.
 * Inputs: None.
 * Outputs: A warning banner when refresh checks fail unexpectedly, plus redirect behavior on session expiry.
 * Behavior: Polls the auth session endpoint on an interval so expired sessions recover into the login flow.
 */
export function SessionHeartbeat(): ReactElement | null {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [warningMessage, setWarningMessage] = useState<string | null>(null);

  const refreshSession = useEffectEvent(async () => {
    try {
      await readCurrentSession();
      setWarningMessage(null);
    } catch (error) {
      if (isSessionAuthError(error)) {
        const currentPath = buildCurrentPath(pathname, searchParams.toString());
        router.replace(
          buildLoginRedirectPath({
            nextPath: currentPath,
            reason: "session-expired",
          }),
        );
        router.refresh();
        return;
      }

      setWarningMessage(
        "Session refresh is temporarily unavailable. Save your place and reload if this persists.",
      );
    }
  });

  useEffect(() => {
    void refreshSession();

    const intervalId = window.setInterval(() => {
      void refreshSession();
    }, HEARTBEAT_INTERVAL_MS);

    return () => {
      window.clearInterval(intervalId);
    };
  }, []);

  if (warningMessage === null) {
    return null;
  }

  return (
    <div className="status-banner warning" role="status">
      {warningMessage}
    </div>
  );
}

function buildCurrentPath(pathname: string, queryString: string): string {
  return queryString.length > 0 ? `${pathname}?${queryString}` : pathname;
}
