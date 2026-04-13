/*
Purpose: Provide the canonical two-column review workspace frame used by dense finance queues.
Scope: Shared primary-and-side rail layout with an optional sticky evidence/action column.
Dependencies: React and desktop application CSS classes that theme the shared layout hooks.
*/

import type { ReactElement, ReactNode } from "react";

export type ReviewLayoutProps = {
  className?: string;
  main: ReactNode;
  side: ReactNode;
  stickySide?: boolean;
};

/**
 * Purpose: Compose a primary review canvas and a neighboring evidence/action rail.
 * Inputs: Main content node, side content node, optional class name, and sticky-side toggle.
 * Outputs: A React section that keeps review actions close to evidence context.
 * Behavior: Defaults the side rail to sticky so queue rows and detail panes remain visually linked while scrolling.
 */
export function ReviewLayout({
  className,
  main,
  side,
  stickySide = true,
}: Readonly<ReviewLayoutProps>): ReactElement {
  return (
    <section className={["review-layout", className].filter(Boolean).join(" ")}>
      <div className="review-layout-main">{main}</div>
      <aside
        className={`review-layout-side ${stickySide ? "review-layout-side-sticky" : ""}`.trim()}
      >
        {side}
      </aside>
    </section>
  );
}
