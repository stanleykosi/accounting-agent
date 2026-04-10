/*
Purpose: Proxy same-origin entity list and create requests from the browser to the FastAPI backend.
Scope: GET and POST forwarding for `/api/entities` while preserving rotated session cookies.
Dependencies: The shared entity proxy helper and Next.js route handlers.
*/

import { proxyEntityRequest } from "../../../lib/entities/proxy";

/**
 * Purpose: Proxy entity list reads from the browser to the backend entity API.
 * Inputs: The incoming Next.js request object.
 * Outputs: The backend entity list response with cookies and JSON preserved.
 * Behavior: Keeps browser-to-backend auth same-origin so session rotation remains safe.
 */
export async function GET(request: Request): Promise<Response> {
  return proxyEntityRequest(request);
}

/**
 * Purpose: Proxy entity create requests from the browser to the backend entity API.
 * Inputs: The incoming Next.js request object.
 * Outputs: The backend workspace creation response with cookies and JSON preserved.
 * Behavior: Forwards POST bodies unchanged so the FastAPI contract stays authoritative.
 */
export async function POST(request: Request): Promise<Response> {
  return proxyEntityRequest(request);
}
