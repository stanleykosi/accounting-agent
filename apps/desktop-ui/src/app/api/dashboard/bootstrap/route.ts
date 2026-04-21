/*
Purpose: Aggregate portfolio bootstrap data for the desktop command center in one same-origin request.
Scope: Authenticated entity list forwarding plus parallel close-run list hydration per entity.
Dependencies: Backend entity URL helpers, close-run response parsing, and shared proxy headers.
*/

import {
  parseCloseRunListResponse,
  type CloseRunSummary,
} from "../../../../lib/close-runs";
import {
  buildBackendEntitiesUrl,
  type EntityListResponse,
  type EntitySummary,
} from "../../../../lib/entities/api";
import { buildEntityProxyHeaders } from "../../../../lib/entities/proxy";

type DashboardBootstrapEntry = Readonly<{
  closeRuns: readonly CloseRunSummary[];
  entity: EntitySummary;
}>;

export async function GET(request: Request): Promise<Response> {
  const proxyHeaders = buildEntityProxyHeaders(request);
  const entityResponse = await fetch(buildBackendEntitiesUrl("/"), {
    cache: "no-store",
    headers: proxyHeaders,
    method: "GET",
    redirect: "manual",
  });

  if (!entityResponse.ok) {
    return forwardBackendFailure(entityResponse);
  }

  const entityPayload = (await entityResponse.json()) as EntityListResponse;

  try {
    const entries = await Promise.all(
      entityPayload.entities.map(async (entity) => ({
        closeRuns: await readCloseRunsForEntity(entity, proxyHeaders),
        entity,
      })),
    );

    return Response.json(
      {
        entries,
      } satisfies { entries: readonly DashboardBootstrapEntry[] },
      {
        headers: {
          "cache-control": "no-store",
        },
      },
    );
  } catch (error: unknown) {
    return Response.json(
      {
        detail: {
          code: "dashboard_bootstrap_failed",
          message:
            error instanceof Error
              ? error.message
              : "The portfolio bootstrap request could not be completed.",
        },
      },
      {
        headers: {
          "cache-control": "no-store",
        },
        status: 502,
      },
    );
  }
}

async function readCloseRunsForEntity(
  entity: EntitySummary,
  proxyHeaders: Headers,
): Promise<readonly CloseRunSummary[]> {
  const response = await fetch(
    buildBackendEntitiesUrl(`/${encodeURIComponent(entity.id)}/close-runs`),
    {
      cache: "no-store",
      headers: proxyHeaders,
      method: "GET",
      redirect: "manual",
    },
  );

  if (!response.ok) {
    const failureResponse = await forwardBackendFailure(response);
    const errorPayload = await safeJson(failureResponse);
    throw new Error(resolveFailureMessage(errorPayload, response.status));
  }

  const payload: unknown = await response.json();
  return parseCloseRunListResponse(payload);
}

async function forwardBackendFailure(response: Response): Promise<Response> {
  const responseHeaders = new Headers();
  responseHeaders.set("cache-control", "no-store");

  const contentType = response.headers.get("content-type");
  if (contentType !== null) {
    responseHeaders.set("content-type", contentType);
  }

  const setCookie = response.headers.get("set-cookie");
  if (setCookie !== null) {
    responseHeaders.append("set-cookie", setCookie);
  }

  return new Response(await response.arrayBuffer(), {
    headers: responseHeaders,
    status: response.status,
    statusText: response.statusText,
  });
}

async function safeJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  try {
    return await response.json();
  } catch {
    return null;
  }
}

function resolveFailureMessage(payload: unknown, statusCode: number): string {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (typeof detail === "string") {
      return detail;
    }

    if (isRecord(detail) && typeof detail.message === "string") {
      return detail.message;
    }
  }

  return `Backend request failed with status ${statusCode}.`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
