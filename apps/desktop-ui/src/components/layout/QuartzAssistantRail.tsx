"use client";

import { useState, type ReactElement, type ReactNode } from "react";
import { QuartzIcon } from "./QuartzIcons";

type QuartzAssistantRailProps = Readonly<{
  children?: ReactNode;
  footer?: ReactNode;
  subtitle: string;
  title?: string;
}>;

export function QuartzAssistantRail({
  children,
  footer,
  subtitle,
  title = "Omni-Assistant",
}: QuartzAssistantRailProps): ReactElement {
  const [isOpen, setIsOpen] = useState(false);

  if (!isOpen) {
    return (
      <aside className="quartz-assistant-rail collapsed" aria-label={title}>
        <button
          aria-expanded="false"
          className="quartz-assistant-toggle"
          onClick={() => setIsOpen(true)}
          type="button"
        >
          <QuartzIcon className="quartz-inline-icon" name="assistant" />
          <span>{title}</span>
        </button>
      </aside>
    );
  }

  return (
    <aside className="quartz-assistant-rail open quartz-right-rail" aria-label={title}>
      <div className="quartz-right-rail-header quartz-assistant-rail-header">
        <div className="quartz-assistant-rail-heading">
          <QuartzIcon className="quartz-inline-icon" name="assistant" />
          <div>
            <h2 className="quartz-right-rail-title">{title}</h2>
            <p className="quartz-right-rail-subtitle">{subtitle}</p>
          </div>
        </div>

        <button
          aria-expanded="true"
          className="quartz-assistant-close-button"
          onClick={() => setIsOpen(false)}
          type="button"
        >
          Close
        </button>
      </div>

      <div className="quartz-right-rail-body">{children}</div>

      {footer ? <div className="quartz-right-rail-footer">{footer}</div> : null}
    </aside>
  );
}
