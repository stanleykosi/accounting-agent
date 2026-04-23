/*
Purpose: Harden same-origin backend proxy calls against short hosted startup and replica warmup gaps.
Scope: Retry safe read requests across transient upstream availability failures and return canonical 503 payloads when the backend is still waking.
Dependencies: Standard Fetch APIs used by Next.js route handlers and server-side helpers.
*/

const READ_RETRYABLE_METHODS = new Set(["GET", "HEAD"]);
const TRANSIENT_STATUS_CODES = new Set([502, 503, 504]);
const RETRY_DELAYS_MS = [150, 350, 700] as const;
const MAX_RETRY_DELAY_MS = 700;

export async function fetchBackendWithAvailabilityRetry(
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const shouldRetry = READ_RETRYABLE_METHODS.has(method);

  for (let attempt = 0; ; attempt += 1) {
    try {
      const response = await fetch(input, init);
      if (
        !shouldRetry ||
        !TRANSIENT_STATUS_CODES.has(response.status) ||
        attempt >= RETRY_DELAYS_MS.length
      ) {
        return response;
      }

      await response.body?.cancel?.();
      await delay(resolveRetryDelayMs(response, attempt));
    } catch (error) {
      if (!shouldRetry || attempt >= RETRY_DELAYS_MS.length || !isTransientFetchError(error)) {
        throw error;
      }

      await delay(RETRY_DELAYS_MS[attempt] ?? MAX_RETRY_DELAY_MS);
    }
  }
}

export function buildBackendUnavailableResponse(): Response {
  return Response.json(
    {
      detail: {
        code: "backend_unavailable",
        message: "The backend service is still starting. Retry shortly.",
      },
    },
    {
      headers: {
        "cache-control": "no-store",
        "retry-after": "1",
      },
      status: 503,
    },
  );
}

function isTransientFetchError(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }

  const message = error.message.toLowerCase();
  return (
    error.name === "TypeError" ||
    message.includes("fetch failed") ||
    message.includes("connection refused") ||
    message.includes("network")
  );
}

function resolveRetryDelayMs(response: Response, attempt: number): number {
  const retryAfter = response.headers.get("retry-after");
  if (retryAfter !== null) {
    const retryAfterSeconds = Number.parseInt(retryAfter, 10);
    if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0) {
      return retryAfterSeconds * 1_000;
    }
  }

  return RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)] ?? MAX_RETRY_DELAY_MS;
}

function delay(durationMs: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, durationMs);
  });
}
