/*
Purpose: Proxy browser chat requests from the Next.js origin to the FastAPI chat API.
Scope: Same-origin thread creation, listing, message sends, and reads without
introducing CORS configuration.
Dependencies: Next.js route handlers and the canonical backend chat URL helpers.
*/

import { buildBackendChatUrl } from "../../../../lib/chat";
import {
  buildBackendUnavailableResponse,
  fetchBackendWithAvailabilityRetry,
} from "../../../../lib/backend-proxy";

export const runtime = "nodejs";
export const maxDuration = 60;

type ChatProxyRouteContext = {
  params: Promise<{
    chatPath: string[];
  }>;
};

/**
 * Purpose: Proxy GET chat requests such as thread listing and detail reads to the FastAPI chat API.
 * Inputs: The incoming Next.js request and the requested chat route segments.
 * Outputs: The backend chat response with cookies and JSON forwarded to the browser.
 * Behavior: Preserves status codes and cookies so the browser stays in sync.
 */
export async function GET(request: Request, context: ChatProxyRouteContext): Promise<Response> {
  return proxyChatRequest(request, context);
}

/**
 * Purpose: Proxy POST chat requests such as thread creation and message sends to the FastAPI chat API.
 * Inputs: The incoming Next.js request and the requested chat route segments.
 * Outputs: The backend chat response with cookies and JSON forwarded to the browser.
 * Behavior: Keeps browser chat traffic same-origin while still using the canonical FastAPI chat routes.
 */
export async function POST(request: Request, context: ChatProxyRouteContext): Promise<Response> {
  return proxyChatRequest(request, context);
}

/**
 * Purpose: Proxy DELETE chat requests such as thread deletion to the FastAPI chat API.
 * Inputs: The incoming Next.js request and the requested chat route segments.
 * Outputs: The backend chat response with cookies and JSON forwarded to the browser.
 * Behavior: Keeps destructive chat actions same-origin while using the canonical backend routes.
 */
export async function DELETE(request: Request, context: ChatProxyRouteContext): Promise<Response> {
  return proxyChatRequest(request, context);
}

async function proxyChatRequest(
  request: Request,
  context: ChatProxyRouteContext,
): Promise<Response> {
  const { chatPath } = await context.params;
  const requestUrl = new URL(request.url);
  const requestBody = request.method === "GET" ? null : await request.text();
  let backendResponse: Response;
  try {
    backendResponse = await fetchBackendWithAvailabilityRetry(
      buildBackendChatUrl(`/${chatPath.join("/")}${requestUrl.search}`),
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
