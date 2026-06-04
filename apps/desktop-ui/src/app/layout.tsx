/*
Purpose: Define the root layout and metadata for the desktop UI application shell.
Scope: Global styles, document metadata, and shared page chrome for the Next.js App Router.
Dependencies: apps/desktop-ui/src/app/globals.css and Next.js metadata support.
*/

import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Accounting AI Agent",
  description:
    "Canonical desktop workspace for close-run driven accounting automation, review, and reporting.",
};

/**
 * Purpose: Wrap every page in the canonical desktop document shell.
 * Inputs: The route segment content that Next.js renders for the current path.
 * Outputs: A hydrated HTML document with global typography and theme classes applied.
 * Behavior: Applies the root language and body class required by the initial design system.
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>): ReactElement {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
