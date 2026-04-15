/*
Purpose: Provide shared Next.js route-handler proxy helpers for the entity workspace API surface.
Scope: Backend request forwarding, cookie/header preservation, and response passthrough for entity routes.
Dependencies: Standard Fetch APIs and the backend entity URL builder in the entity API helper module.
*/

import { buildBackendEntitiesUrl } from "./api";

/**
 * Purpose: Proxy an incoming browser request to the canonical FastAPI entity route.
 * Inputs: The raw Next.js request plus the path segments under `/api/entities`.
 * Outputs: A Response that preserves the backend status, JSON body, and rotated cookies.
 * Behavior: Forwards auth cookies and user-agent headers so the backend session model stays authoritative.
 */
export async function proxyEntityRequest(
  request: Request,
  pathSegments: readonly string[] = [],
): Promise<Response> {
  const requestUrl = new URL(request.url);
  const requestBody =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();
  const backendResponse = await fetch(
    buildBackendEntitiesUrl(`/${pathSegments.join("/")}`, requestUrl.search),
    {
      cache: "no-store",
      headers: buildProxyHeaders(request),
      method: request.method,
      redirect: "manual",
      ...(requestBody === undefined ? {} : { body: requestBody }),
    },
  );

  const responseHeaders = new Headers();
  for (const headerName of [
    "cache-control",
    "content-disposition",
    "content-length",
    "content-type",
    "etag",
    "last-modified",
  ]) {
    const headerValue = backendResponse.headers.get(headerName);
    if (headerValue !== null) {
      responseHeaders.set(headerName, headerValue);
    }
  }

  const setCookie = backendResponse.headers.get("set-cookie");
  if (setCookie !== null) {
    responseHeaders.append("set-cookie", setCookie);
  }

  return new Response(await backendResponse.arrayBuffer(), {
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
