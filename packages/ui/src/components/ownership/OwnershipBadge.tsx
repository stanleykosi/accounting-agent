/*
Purpose: Render compact ownership, lock, and last-touch metadata for review rows.
Scope: Reusable badge for documents, recommendations, and review targets in dense workflows.
Dependencies: React CSSProperties types only; data shape mirrors the ownership API response.
*/

import type { CSSProperties, ReactElement } from "react";

export type OwnershipBadgeOperator = {
  email: string;
  full_name: string;
  id: string;
};

export type OwnershipBadgeProps = {
  lastTouchedAt?: string | null;
  lastTouchedBy?: OwnershipBadgeOperator | null;
  lockedAt?: string | null;
  lockedBy?: OwnershipBadgeOperator | null;
  owner?: OwnershipBadgeOperator | null;
  tone?: "compact" | "default";
};

const badgeStyle: CSSProperties = {
  alignItems: "center",
  border: "1px solid rgba(76, 139, 245, 0.28)",
  borderRadius: "8px",
  color: "#F4F7FB",
  display: "inline-flex",
  fontSize: "12px",
  fontWeight: 700,
  gap: "8px",
  lineHeight: "18px",
  maxWidth: "100%",
  minHeight: "28px",
  overflow: "hidden",
  padding: "4px 8px",
  whiteSpace: "nowrap",
};

const unlockedStyle: CSSProperties = {
  background: "#182338",
};

const lockedStyle: CSSProperties = {
  background: "#D9534F",
  borderColor: "rgba(255, 255, 255, 0.22)",
};

const textStyle: CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
};

const mutedTextStyle: CSSProperties = {
  color: "#B7C3D6",
  fontWeight: 600,
};

/**
 * Purpose: Show who owns, locked, or last touched a workflow item.
 * Inputs: Optional owner, lock holder, last-touch operator, and compact/default rendering tone.
 * Outputs: A stable inline badge suitable for review tables and evidence panes.
 * Behavior: Prioritizes active lock information, then owner, then last-touch metadata.
 */
export function OwnershipBadge({
  lastTouchedAt,
  lastTouchedBy,
  lockedAt,
  lockedBy,
  owner,
  tone = "default",
}: Readonly<OwnershipBadgeProps>): ReactElement {
  const activeOperator = lockedBy ?? owner ?? lastTouchedBy;
  const label = buildBadgeLabel({ lastTouchedBy, lockedBy, owner });
  const title = buildBadgeTitle({ lastTouchedAt, lastTouchedBy, lockedAt, lockedBy, owner });

  return (
    <span
      aria-label={label}
      style={{
        ...badgeStyle,
        ...(lockedBy ? lockedStyle : unlockedStyle),
        ...(tone === "compact" ? { minHeight: "24px", padding: "2px 8px" } : {}),
      }}
      title={title}
    >
      <span aria-hidden="true">{lockedBy ? "Locked" : "Owner"}</span>
      <span style={textStyle}>{activeOperator?.full_name ?? "Unassigned"}</span>
      {!lockedBy && owner === null && lastTouchedBy !== null && lastTouchedBy !== undefined ? (
        <span style={mutedTextStyle}>last touch</span>
      ) : null}
    </span>
  );
}

/**
 * Purpose: Build accessible text that summarizes the badge's active ownership state.
 * Inputs: Optional owner, lock holder, and last-touch operator.
 * Outputs: A concise aria label.
 * Behavior: Prioritizes lock holder, then owner, then last-touch metadata.
 */
function buildBadgeLabel(
  state: Readonly<{
    lastTouchedBy?: OwnershipBadgeOperator | null | undefined;
    lockedBy?: OwnershipBadgeOperator | null | undefined;
    owner?: OwnershipBadgeOperator | null | undefined;
  }>,
): string {
  if (state.lockedBy) {
    return `Locked by ${state.lockedBy.full_name}`;
  }
  if (state.owner) {
    return `Owned by ${state.owner.full_name}`;
  }
  if (state.lastTouchedBy) {
    return `Last touched by ${state.lastTouchedBy.full_name}`;
  }

  return "No owner assigned";
}

/**
 * Purpose: Build hover text with slightly richer ownership context.
 * Inputs: Optional owner, lock holder, last-touch operator, and timestamps.
 * Outputs: Browser title text for the rendered badge.
 * Behavior: Includes timestamps only when the API provided them.
 */
function buildBadgeTitle(
  state: Readonly<{
    lastTouchedAt?: string | null | undefined;
    lastTouchedBy?: OwnershipBadgeOperator | null | undefined;
    lockedAt?: string | null | undefined;
    lockedBy?: OwnershipBadgeOperator | null | undefined;
    owner?: OwnershipBadgeOperator | null | undefined;
  }>,
): string {
  if (state.lockedBy) {
    return `Locked by ${state.lockedBy.full_name}${formatOptionalTimestamp(state.lockedAt)}`;
  }
  if (state.owner) {
    return `Owned by ${state.owner.full_name}`;
  }
  if (state.lastTouchedBy) {
    return `Last touched by ${state.lastTouchedBy.full_name}${formatOptionalTimestamp(
      state.lastTouchedAt,
    )}`;
  }

  return "No owner or last-touch metadata";
}

/**
 * Purpose: Format an optional ISO timestamp for compact title text.
 * Inputs: Optional timestamp string from the ownership API.
 * Outputs: A prefixed timestamp fragment or an empty string.
 * Behavior: Leaves localization to later presentation layers.
 */
function formatOptionalTimestamp(value?: string | null): string {
  return value ? ` at ${value}` : "";
}
