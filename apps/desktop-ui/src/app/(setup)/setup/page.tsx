/*
Purpose: Render the first-run setup experience used by the desktop shell before operators enter protected workspace routes.
Scope: Local runtime readiness messaging, setup guidance, and the live health checklist for loopback services.
Dependencies: Shared UI surfaces, the setup health helper, and the setup checklist client component.
*/

import { SurfaceCard } from "@accounting-ai-agent/ui";
import type { ReactElement } from "react";
import { HealthChecklist } from "../../../components/setup/HealthChecklist";
import { readDesktopSetupHealth } from "../../../lib/setup/health";

type SetupPageProps = {
  searchParams: Promise<{
    next?: string;
  }>;
};

/**
 * Purpose: Provide the canonical desktop setup surface shown when the local runtime is not yet ready.
 * Inputs: The optional protected route to continue to once the setup checks pass.
 * Outputs: A server-rendered setup page with the current runtime snapshot and recovery guidance.
 * Behavior: Reads live loopback health status on the server so the first render already reflects the current machine state.
 */
export default async function SetupPage({
  searchParams,
}: Readonly<SetupPageProps>): Promise<ReactElement> {
  const resolvedSearchParams = await searchParams;
  const nextPath = sanitizeNextPath(resolvedSearchParams.next) ?? "/";
  const snapshot = await readDesktopSetupHealth();
  const isHostedMode = snapshot.mode === "hosted";

  return (
    <main className="setup-shell">
      <section className="setup-hero-grid">
        <div className="setup-hero-copy">
          <p className="eyebrow">{isHostedMode ? "Hosted Frontend" : "Desktop Setup"}</p>
          <h1>
            {isHostedMode
              ? "This frontend is already running in hosted mode."
              : "Validate the local accounting runtime before you enter the workspace."}
          </h1>
          <p className="lede">
            {isHostedMode
              ? "The hosted browser and remote desktop shells do not require loopback Redis, PostgreSQL, or storage checks inside the frontend. The only canonical path is the hosted web application talking to the Railway backend."
              : "The packaged desktop shell runs the Next.js UI locally and fails closed when the demo stack is unavailable. Start the canonical services first so close-run review, evidence tracing, and background jobs all land on one healthy path."}
          </p>
        </div>

        <SurfaceCard title="Required services" subtitle="Why the shell pauses here" tone="accent">
          <div className="detail-block">
            <p className="form-helper">
              {isHostedMode
                ? "Hosted mode skips the local setup gate entirely. Browser and remote Tauri shells should point at the hosted frontend and let Next.js proxy all authenticated API traffic to Railway."
                : "The setup gate checks the loopback API, MinIO object storage, PostgreSQL, and Redis before the main workflow UI opens."}
            </p>
            <ul className="detail-list">
              {isHostedMode ? (
                <>
                  <li>The hosted frontend should be deployed on Vercel or another public web origin.</li>
                  <li>The backend API, worker, Redis, storage, and Postgres remain behind Railway.</li>
                  <li>The protected workspace route resumes at {nextPath} without any loopback dependency checks.</li>
                </>
              ) : (
                <>
                  <li>
                    The desktop shell never invents fallback infrastructure or silent retry paths.
                  </li>
                  <li>
                    Operators recover from one canonical script flow instead of mixed local states.
                  </li>
                  <li>
                    The protected workspace route resumes at {nextPath} once the runtime is ready.
                  </li>
                </>
              )}
            </ul>
          </div>
        </SurfaceCard>
      </section>

      <HealthChecklist initialSnapshot={snapshot} nextPath={nextPath} />
    </main>
  );
}

function sanitizeNextPath(value: string | undefined): string | null {
  if (typeof value !== "string" || value.length === 0) {
    return null;
  }

  if (!value.startsWith("/") || value.startsWith("//")) {
    return null;
  }

  return value;
}
