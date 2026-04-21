/*
Purpose: Persist the operator's latest close-run navigation context for shell shortcuts.
Scope: Browser-side close mission control/chat targets used by sidebar navigation outside an active close route.
Dependencies: sessionStorage and same-tab custom events only.
*/

import type { DashboardEntityRuns } from "./dashboard";

export type RememberedCloseContext = Readonly<{
  chatHref: string;
  closeRunId: string;
  entityId: string;
  overviewHref: string;
}>;

const LAST_CLOSE_CONTEXT_EVENT = "accounting-ai-agent:last-close-context";
const LAST_CLOSE_CONTEXT_STORAGE_KEY = "accounting-ai-agent:last-close-context";

export function buildRememberedCloseContext(
  entityId: string,
  closeRunId: string,
): RememberedCloseContext {
  return {
    chatHref: `/entities/${entityId}/close-runs/${closeRunId}/chat`,
    closeRunId,
    entityId,
    overviewHref: `/entities/${entityId}/close-runs/${closeRunId}`,
  };
}

export function deriveRememberedCloseContextFromDashboardEntries(
  entries: readonly DashboardEntityRuns[],
): RememberedCloseContext | null {
  const preferredCloseRun =
    entries
      .flatMap((entry) =>
        entry.closeRuns.map((closeRun) => ({
          closeRun,
          entityId: entry.entity.id,
        })),
      )
      .sort(
        (left, right) =>
          new Date(right.closeRun.updatedAt).valueOf() -
          new Date(left.closeRun.updatedAt).valueOf(),
      )[0] ?? null;

  return preferredCloseRun === null
    ? null
    : buildRememberedCloseContext(preferredCloseRun.entityId, preferredCloseRun.closeRun.id);
}

export function readRememberedCloseContext(): RememberedCloseContext | null {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const rawValue = window.sessionStorage.getItem(LAST_CLOSE_CONTEXT_STORAGE_KEY);
    if (rawValue === null) {
      return null;
    }

    const parsedValue = JSON.parse(rawValue) as Partial<RememberedCloseContext>;
    if (
      typeof parsedValue.entityId !== "string" ||
      typeof parsedValue.closeRunId !== "string" ||
      typeof parsedValue.overviewHref !== "string" ||
      typeof parsedValue.chatHref !== "string"
    ) {
      return null;
    }

    return {
      chatHref: parsedValue.chatHref,
      closeRunId: parsedValue.closeRunId,
      entityId: parsedValue.entityId,
      overviewHref: parsedValue.overviewHref,
    };
  } catch {
    return null;
  }
}

export function subscribeRememberedCloseContext(
  listener: (context: RememberedCloseContext) => void,
): () => void {
  if (typeof window === "undefined") {
    return () => undefined;
  }

  const handler = (event: Event): void => {
    const customEvent = event as CustomEvent<RememberedCloseContext>;
    if (customEvent.detail) {
      listener(customEvent.detail);
    }
  };

  window.addEventListener(LAST_CLOSE_CONTEXT_EVENT, handler);
  return () => {
    window.removeEventListener(LAST_CLOSE_CONTEXT_EVENT, handler);
  };
}

export function writeRememberedCloseContext(context: Readonly<RememberedCloseContext>): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.sessionStorage.setItem(
      LAST_CLOSE_CONTEXT_STORAGE_KEY,
      JSON.stringify(context),
    );
  } catch {
    // Ignore storage failures; the in-memory event still updates active listeners.
  }

  window.dispatchEvent(
    new CustomEvent<RememberedCloseContext>(LAST_CLOSE_CONTEXT_EVENT, {
      detail: context,
    }),
  );
}
