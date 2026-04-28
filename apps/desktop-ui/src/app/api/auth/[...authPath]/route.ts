/*
Purpose: Proxy browser auth requests from the Next.js origin to the FastAPI auth API.
Scope: Same-origin login, registration, logout, and session reads without introducing CORS configuration.
Dependencies: Next.js route handlers and the canonical backend auth URL helpers.
*/

import { buildBackendAuthUrl } from "../../../../lib/auth/session";
import {
  buildBackendUnavailableResponse,
  fetchBackendWithAvailabilityRetry,
} from "../../../../lib/backend-proxy";

type AuthProxyRouteContext = {
  params: Promise<{
    authPath: string[];
  }>;
};

/**
 * Purpose: Proxy GET auth requests such as current-session reads to the FastAPI auth API.
 * Inputs: The incoming Next.js request and the requested auth route segments.
 * Outputs: The backend auth response with cookies and JSON forwarded to the browser.
 * Behavior: Preserves status codes and rotated session cookies so the browser stays in sync.
 */
export async function GET(request: Request, context: AuthProxyRouteContext): Promise<Response> {
  return proxyAuthRequest(request, context);
}

/**
 * Purpose: Proxy POST auth requests such as login, registration, and logout to the FastAPI auth API.
 * Inputs: The incoming Next.js request and the requested auth route segments.
 * Outputs: The backend auth response with cookies and JSON forwarded to the browser.
 * Behavior: Keeps browser auth traffic same-origin while still using the canonical FastAPI auth routes.
 */
export async function POST(request: Request, context: AuthProxyRouteContext): Promise<Response> {
  return proxyAuthRequest(request, context);
}

async function proxyAuthRequest(
  request: Request,
  context: AuthProxyRouteContext,
): Promise<Response> {
  const { authPath } = await context.params;
  const requestBody = request.method === "GET" ? null : await request.text();
  let backendResponse: Response;
  try {
    backendResponse = await fetchBackendWithAvailabilityRetry(
      buildBackendAuthUrl(`/${authPath.join("/")}`),
      {
        cache: "no-store",
        headers: buildProxyHeaders(request),
        method: request.method,
        redirect: "manual",
        ...(requestBody === null ? {} : { body: requestBody }),
      },
    );
  } catch {
    return buildBackendUnavailableResponse();
  }

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
