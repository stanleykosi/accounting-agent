/*
Purpose: Expose a same-origin setup-health endpoint that the desktop setup page and middleware can query safely.
Scope: Server-side runtime checks for loopback services required before entering the main workflow UI.
Dependencies: Next.js route handlers and the server-only setup health helper.
*/

import { NextResponse } from "next/server";
import { readDesktopSetupHealth } from "../../../../lib/setup/health";

export const runtime = "nodejs";

/**
 * Purpose: Return the current desktop runtime readiness snapshot.
 * Inputs: None.
 * Outputs: JSON describing whether the local services required by the desktop shell are reachable.
 * Behavior: Disables caching so setup checks always reflect the current loopback runtime state.
 */
export async function GET(): Promise<NextResponse> {
  const snapshot = await readDesktopSetupHealth();
  return NextResponse.json(snapshot, {
    headers: {
      "cache-control": "no-store",
    },
  });
}
