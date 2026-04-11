/*
Purpose: Proxy same-origin ownership requests from the browser to the FastAPI backend.
Scope: GET and POST forwarding for `/api/ownership/{entityId}/**` while preserving cookies.
Dependencies: Backend ownership URL builder and Next.js route handlers.
*/

import { buildBackendOwnershipUrl } from "../../../../lib/ownership";

type OwnershipProxyRouteContext = {
  params: Promise<{
    ownershipPath: string[];
  }>;
};

/**
 * Purpose: Proxy ownership reads from the browser to the backend ownership API.
 * Inputs: The incoming request and path segments after `/api/ownership`.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Requires the first path segment to be the entity UUID to keep routing explicit.
 */
export async function GET(
  request: Request,
  context: OwnershipProxyRouteContext,
): Promise<Response> {
  const { ownershipPath } = await context.params;
  return proxyOwnershipRequest(request, ownershipPath);
}

/**
 * Purpose: Proxy ownership mutations from the browser to the backend ownership API.
 * Inputs: The incoming request and path segments after `/api/ownership`.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Leaves lock and last-touch validation in the canonical FastAPI service.
 */
export async function POST(
  request: Request,
  context: OwnershipProxyRouteContext,
): Promise<Response> {
  const { ownershipPath } = await context.params;
  return proxyOwnershipRequest(request, ownershipPath);
}

/**
 * Purpose: Forward an ownership proxy request to the canonical FastAPI route.
 * Inputs: Raw browser request and path segments under `/api/ownership`.
 * Outputs: Response preserving backend status, content type, body, and rotated cookies.
 * Behavior: Fails fast when the required entity segment is absent.
 */
async function proxyOwnershipRequest(
  request: Request,
  pathSegments: readonly string[],
): Promise<Response> {
  if (pathSegments.length < 1) {
    return Response.json(
      {
        detail: {
          code: "target_scope_invalid",
          message: "Ownership proxy routes require an entity ID path segment.",
        },
      },
      { status: 422 },
    );
  }

  const entityId = pathSegments[0];
  if (entityId === undefined) {
    return Response.json(
      {
        detail: {
          code: "target_scope_invalid",
          message: "Ownership proxy routes require an entity ID path segment.",
        },
      },
      { status: 422 },
    );
  }
  const backendPathSegments = pathSegments.slice(1);
  const requestUrl = new URL(request.url);
  const backendResponse = await fetch(
    buildBackendOwnershipUrl(entityId, `/${backendPathSegments.join("/")}`, requestUrl.search),
    {
      cache: "no-store",
      headers: buildProxyHeaders(request),
      method: request.method,
      redirect: "manual",
      ...(request.method === "GET" ? {} : { body: await request.text() }),
    },
  );

  const responseHeaders = new Headers();
  const contentType = backendResponse.headers.get("content-type");
  const setCookie = backendResponse.headers.get("set-cookie");
  if (contentType !== null) {
    responseHeaders.set("content-type", contentType);
  }

  if (setCookie !== null) {
    responseHeaders.append("set-cookie", setCookie);
  }

  return new Response(await backendResponse.text(), {
    headers: responseHeaders,
    status: backendResponse.status,
    statusText: backendResponse.statusText,
  });
}

/**
 * Purpose: Build the restricted header set forwarded to the backend API.
 * Inputs: Incoming browser request.
 * Outputs: Headers containing only content negotiation, auth cookies, and user-agent context.
 * Behavior: Avoids forwarding arbitrary browser headers into the local FastAPI surface.
 */
function buildProxyHeaders(request: Request): Headers {
  const headers = new Headers({
    Accept: request.headers.get("accept") ?? "application/json",
  });
  const contentType = request.headers.get("content-type");
  const cookie = request.headers.get("cookie");
  const userAgent = request.headers.get("user-agent");

  if (contentType !== null) {
    headers.set("content-type", contentType);
  }

  if (cookie !== null) {
    headers.set("cookie", cookie);
  }

  if (userAgent !== null) {
    headers.set("user-agent", userAgent);
  }

  return headers;
}
