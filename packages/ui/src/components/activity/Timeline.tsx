/*
Purpose: Render a reusable activity timeline for workspace and close-run context.
Scope: Ordered activity items, timestamp display, empty states, and small badge emphasis.
Dependencies: React rendering only so both server and client desktop routes can reuse it.
*/

import type { CSSProperties, ReactElement } from "react";

export type TimelineItemTone = "default" | "success" | "warning";

export type TimelineItem = Readonly<{
  badge?: string;
  detail?: string;
  id: string;
  timestamp: string;
  title: string;
  tone?: TimelineItemTone;
}>;

export type TimelineProps = Readonly<{
  emptyMessage?: string;
  items: readonly TimelineItem[];
}>;

const paletteByTone: Readonly<Record<TimelineItemTone, Readonly<{ dot: string; pill: string }>>> = {
  default: {
    dot: "#146c63",
    pill: "rgba(214, 230, 221, 0.72)",
  },
  success: {
    dot: "#0f6b45",
    pill: "rgba(31, 169, 113, 0.14)",
  },
  warning: {
    dot: "#8d4a02",
    pill: "rgba(217, 119, 6, 0.14)",
  },
};

const listStyle: CSSProperties = {
  display: "grid",
  gap: "16px",
  listStyle: "none",
  margin: 0,
  padding: 0,
};

/**
 * Purpose: Render a compact vertical timeline of activity or lifecycle events.
 * Inputs: Timeline items with titles, timestamps, optional detail, and optional badge labels.
 * Outputs: A React element that presents recent work in descending priority order.
 * Behavior: Falls back to a quiet empty state when no activity items are supplied.
 */
export function Timeline({
  emptyMessage = "No activity has been recorded yet.",
  items,
}: TimelineProps): ReactElement {
  if (items.length === 0) {
    return (
      <p
        style={{
          border: "1px dashed rgba(52, 72, 63, 0.18)",
          borderRadius: "16px",
          color: "#4d5b52",
          margin: 0,
          padding: "16px",
        }}
      >
        {emptyMessage}
      </p>
    );
  }

  return (
    <ol style={listStyle}>
      {items.map((item) => {
        const palette = paletteByTone[item.tone ?? "default"];
        return (
          <li
            key={item.id}
            style={{
              display: "grid",
              gap: "8px",
              gridTemplateColumns: "18px minmax(0, 1fr)",
            }}
          >
            <span
              aria-hidden="true"
              style={{
                alignSelf: "start",
                background: palette.dot,
                borderRadius: "999px",
                height: "10px",
                marginTop: "10px",
                width: "10px",
              }}
            />
            <article
              style={{
                borderBottom: "1px solid rgba(52, 72, 63, 0.1)",
                display: "grid",
                gap: "6px",
                paddingBottom: "16px",
              }}
            >
              <div
                style={{
                  alignItems: "center",
                  display: "flex",
                  flexWrap: "wrap",
                  gap: "10px",
                  justifyContent: "space-between",
                }}
              >
                <strong style={{ fontSize: "1rem", letterSpacing: "-0.02em" }}>{item.title}</strong>
                <span style={{ color: "#4d5b52", fontSize: "0.88rem" }}>{item.timestamp}</span>
              </div>

              {item.badge ? (
                <span
                  style={{
                    alignItems: "center",
                    background: palette.pill,
                    borderRadius: "999px",
                    display: "inline-flex",
                    fontSize: "0.74rem",
                    fontWeight: 700,
                    minHeight: "26px",
                    padding: "0 10px",
                    width: "fit-content",
                  }}
                >
                  {item.badge}
                </span>
              ) : null}

              {item.detail ? (
                <p style={{ color: "#4d5b52", lineHeight: 1.65, margin: 0 }}>{item.detail}</p>
              ) : null}
            </article>
          </li>
        );
      })}
    </ol>
  );
}
