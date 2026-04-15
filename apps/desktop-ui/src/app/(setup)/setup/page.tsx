/*
Purpose: Render the runtime readiness experience before operators enter protected workspace routes.
Scope: Hosted and self-managed runtime readiness messaging, setup guidance, and the live health checklist when loopback services are used.
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
 * Purpose: Provide the canonical runtime setup surface shown when the selected deployment path is not yet ready.
 * Inputs: The optional protected route to continue to once the setup checks pass.
 * Outputs: A server-rendered setup page with the current runtime snapshot and recovery guidance.
 * Behavior: Reads live runtime health status on the server so the first render already reflects the current deployment path.
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
              : "Validate the self-managed runtime before you enter the workspace."}
          </h1>
          <p className="lede">
            {isHostedMode
              ? "Hosted browser and remote desktop clients do not require loopback Redis, PostgreSQL, or storage checks inside the frontend. The canonical hosted path is the deployed web application talking to the Railway backend."
              : "Self-managed desktop or local web runtimes fail closed when required services are unavailable. Start the canonical services first so close-run review, evidence tracing, and background jobs all land on one healthy path."}
          </p>
        </div>

        <SurfaceCard title="Required services" subtitle="Why the shell pauses here" tone="accent">
          <div className="detail-block">
            <p className="form-helper">
              {isHostedMode
                ? "Hosted mode skips the loopback setup gate entirely. Browser and remote desktop clients should point at the hosted frontend and let Next.js proxy authenticated API traffic to Railway."
                : "The setup gate checks the API, object storage, PostgreSQL, and Redis before the main workflow UI opens."}
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
                    The application never invents fallback infrastructure or silent retry paths.
                  </li>
                  <li>
                    Operators recover from one canonical infrastructure path instead of mixed runtime states.
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
