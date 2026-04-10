/*
Purpose: Render the canonical desktop logout action for protected workspace layouts.
Scope: Client-side session revocation and navigation back to the login screen.
Dependencies: Next.js navigation and the same-origin auth proxy client helpers.
*/

"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition, type ReactElement } from "react";
import { logoutUser } from "../../lib/auth/client";

/**
 * Purpose: Provide a resilient logout button for the authenticated workspace shell.
 * Inputs: None.
 * Outputs: A client-rendered button that revokes the session and routes back to login.
 * Behavior: Treats logout as best-effort idempotent and still returns the operator to the auth screen.
 */
export function LogoutButton(): ReactElement {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const handleLogout = (): void => {
    setErrorMessage(null);
    startTransition(async () => {
      try {
        await logoutUser();
      } catch {
        // The server session may already be gone; returning to login is still the correct recovery path.
      } finally {
        router.replace("/login");
        router.refresh();
      }
    });
  };

  return (
    <div className="logout-action">
      <button className="secondary-button" onClick={handleLogout} type="button">
        {isPending ? "Signing out..." : "Sign out"}
      </button>
      {errorMessage ? (
        <p className="inline-feedback" role="alert">
          {errorMessage}
        </p>
      ) : null}
    </div>
  );
}
