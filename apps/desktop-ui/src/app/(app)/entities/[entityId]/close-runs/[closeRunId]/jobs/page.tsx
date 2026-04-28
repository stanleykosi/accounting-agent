/*
Purpose: Render a close-run scoped job monitor for background accounting workflows.
Scope: Job listing, status inspection, cancellation, and resume actions for operators.
Dependencies: Same-origin job APIs and shared desktop surface cards.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import {
  cancelJob,
  JobApiError,
  listEntityJobs,
  resumeJob,
  type JobSummary,
} from "../../../../../../../lib/jobs";
import { requireRouteParam } from "../../../../../../../lib/route-params";

export default function JobsPage(): ReactElement {
  const routeParams = useParams<{ closeRunId: string; entityId: string }>();
  const closeRunId = requireRouteParam(routeParams.closeRunId, "closeRunId");
  const entityId = requireRouteParam(routeParams.entityId, "entityId");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [jobs, setJobs] = useState<readonly JobSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  const refreshJobs = useCallback(async (): Promise<void> => {
    setIsLoading(true);
    try {
      const nextJobs = await listEntityJobs(entityId, { closeRunId });
      setJobs(nextJobs);
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveJobErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId]);

  useEffect(() => {
    void refreshJobs();
  }, [refreshJobs]);

  async function handleJobAction(jobId: string, action: "cancel" | "resume"): Promise<void> {
    try {
      if (action === "cancel") {
        await cancelJob(entityId, jobId, "Canceled from hosted close-run monitor.");
      } else {
        await resumeJob(entityId, jobId, "Resumed from hosted close-run monitor.");
      }
      await refreshJobs();
    } catch (error: unknown) {
      setErrorMessage(resolveJobErrorMessage(error));
    }
  }

  return (
    <div className="app-shell jobs-page">
      <section className="hero-grid close-run-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Workflow Jobs</p>
          <h1>Background execution monitor</h1>
          <p className="lede">
            Track parsing, recommendation generation, reconciliation, and reporting jobs for this
            close run from one production control surface.
          </p>
        </div>
        <SurfaceCard title="Refresh" subtitle="Operator controls" tone="accent">
          <button
            className="primary-button"
            onClick={() => {
              void refreshJobs();
            }}
            type="button"
          >
            Refresh jobs
          </button>
        </SurfaceCard>
      </section>

      {errorMessage ? (
        <div className="status-banner danger" role="alert">
          {errorMessage}
        </div>
      ) : null}

      <SurfaceCard title="Close-run Jobs" subtitle={`${jobs.length} tracked jobs`}>
        {isLoading ? <p className="form-helper">Loading jobs...</p> : null}
        {!isLoading && jobs.length === 0 ? (
          <p className="form-helper">No jobs are recorded for this close run yet.</p>
        ) : null}
        <div className="dashboard-row-list">
          {jobs.map((job) => (
            <article className="dashboard-row" key={job.id}>
              <div className="close-run-row-header">
                <div>
                  <strong className="close-run-row-title">{job.task_name}</strong>
                  <p className="close-run-row-meta">
                    {job.status.replaceAll("_", " ")} • {job.queue_name} • attempts{" "}
                    {job.attempt_count}/{job.max_retries + 1}
                  </p>
                </div>
              </div>
              <p className="form-helper">
                {job.failure_reason ?? job.blocking_reason ?? "No failure or blocker recorded."}
              </p>
              <div className="close-run-link-row">
                <button
                  className="secondary-button"
                  disabled={!["queued", "running", "blocked"].includes(job.status)}
                  onClick={() => {
                    void handleJobAction(job.id, "cancel");
                  }}
                  type="button"
                >
                  Cancel
                </button>
                <button
                  className="secondary-button"
                  disabled={!["failed", "blocked", "canceled"].includes(job.status)}
                  onClick={() => {
                    void handleJobAction(job.id, "resume");
                  }}
                  type="button"
                >
                  Resume
                </button>
              </div>
            </article>
          ))}
        </div>
      </SurfaceCard>
    </div>
  );
}

function resolveJobErrorMessage(error: unknown): string {
  if (error instanceof JobApiError) {
    return error.message;
  }
  return "The job monitor request failed. Reload and try again.";
}
