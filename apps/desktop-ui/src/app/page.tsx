/*
Purpose: Redirect the root desktop route into the canonical protected workspace.
Scope: Root-route handoff between the login page and the authenticated workspace shell.
Dependencies: Next.js navigation redirects and the middleware-authenticated routing contract.
*/

import { redirect } from "next/navigation";

/**
 * Purpose: Route the root URL into the canonical authenticated workspace path.
 * Inputs: None.
 * Outputs: A redirect response handled by the Next.js App Router.
 * Behavior: Leaves authentication decisions to middleware while keeping one canonical workspace route.
 */
export default function HomePage(): never {
  redirect("/workspace");
}
