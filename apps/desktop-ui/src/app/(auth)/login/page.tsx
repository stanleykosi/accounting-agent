/*
Purpose: Render the server entry point for the canonical desktop login route.
Scope: Search-param resolution for session recovery and handoff into the interactive auth screen.
Dependencies: The client-side login screen component and Next.js App Router page props.
*/

import type { ReactElement } from "react";
import { LoginScreen } from "../../../components/auth/LoginScreen";

type LoginPageProps = {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

/**
 * Purpose: Resolve login-route search params on the server before rendering the client auth screen.
 * Inputs: Optional App Router search params containing the preserved next path and session-recovery reason.
 * Outputs: A client login screen seeded with deterministic auth recovery context.
 * Behavior: Keeps the route build-safe by avoiding direct `useSearchParams()` usage in the page component.
 */
export default async function LoginPage({
  searchParams,
}: Readonly<LoginPageProps>): Promise<ReactElement> {
  const resolvedSearchParams = searchParams ? await searchParams : {};

  return (
    <LoginScreen
      initialNextPath={readStringParam(resolvedSearchParams.next)}
      initialReason={readStringParam(resolvedSearchParams.reason)}
    />
  );
}

function readStringParam(value: string | string[] | undefined): string | null {
  if (typeof value === "string") {
    return value;
  }

  if (Array.isArray(value)) {
    return typeof value[0] === "string" ? value[0] : null;
  }

  return null;
}
