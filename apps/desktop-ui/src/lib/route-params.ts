/*
Purpose: Provide one canonical client-side route-param assertion helper for App Router pages.
Scope: Dynamic entity and close-run page params resolved from `useParams()`.
Dependencies: None.
*/

/**
 * Purpose: Resolve one required dynamic route segment into a stable string.
 * Inputs: The raw App Router param value and the expected segment name.
 * Outputs: A non-empty string route param.
 * Behavior: Fails fast when the current route does not provide the required segment.
 */
export function requireRouteParam(
  value: string | readonly string[] | undefined,
  fieldName: string,
): string {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  if (Array.isArray(value) && typeof value[0] === "string" && value[0].length > 0) {
    return value[0];
  }

  throw new Error(`Missing required route param: ${fieldName}`);
}
