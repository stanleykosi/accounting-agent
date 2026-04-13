/*
Purpose: Provide the canonical desktop shell used by authenticated workspace routes.
Scope: Shared header framing, navigation placement, operator identity slotting, and content container layout.
Dependencies: React plus the desktop workspace CSS classes defined by the consuming application.
*/

import type { ReactElement, ReactNode } from "react";

export type AppShellNavigationItem = Readonly<{
  href: string;
  label: string;
}>;

export type AppShellProps = Readonly<{
  brandEyebrow: string;
  brandSubtitle: string;
  brandTitle: string;
  children: ReactNode;
  commandPalette?: ReactNode;
  navigationItems: readonly AppShellNavigationItem[];
  userPanel?: ReactNode;
  utilityPanel?: ReactNode;
}>;

/**
 * Purpose: Render the shared desktop shell around authenticated workspace pages.
 * Inputs: Brand copy, navigation items, optional command and operator panels, and the main page content.
 * Outputs: A React element that applies the canonical workspace header and content container.
 * Behavior: Keeps the shell generic so the desktop app can provide Next.js-specific links and controls as slots.
 */
export function AppShell({
  brandEyebrow,
  brandSubtitle,
  brandTitle,
  children,
  commandPalette,
  navigationItems,
  userPanel,
  utilityPanel,
}: AppShellProps): ReactElement {
  return (
    <div className="workspace-shell">
      <header className="workspace-topbar">
        <div className="workspace-brand">
          <p className="eyebrow">{brandEyebrow}</p>
          <h1>{brandTitle}</h1>
          <p className="workspace-subtitle">{brandSubtitle}</p>
        </div>

        <div className="workspace-actions">
          <nav aria-label="Primary workspace navigation" className="workspace-nav">
            {navigationItems.map((item) => (
              <a className="workspace-link" href={item.href} key={item.href}>
                {item.label}
              </a>
            ))}
          </nav>

          {commandPalette}
          {userPanel}
          {utilityPanel}
        </div>
      </header>

      <main className="workspace-main">{children}</main>
    </div>
  );
}
