/*
Purpose: Provide a lightweight keyboard-first command palette for desktop navigation.
Scope: Command search, keyboard shortcuts, modal rendering, and route-link presentation.
Dependencies: React state/effect hooks plus browser location navigation through anchor links.
*/

"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactElement,
} from "react";

export type CommandPaletteItem = Readonly<{
  description: string;
  href: string;
  id: string;
  keywords?: readonly string[];
  label: string;
}>;

export type CommandPaletteProps = Readonly<{
  items: readonly CommandPaletteItem[];
  triggerLabel?: string;
}>;

const overlayStyle: CSSProperties = {
  alignItems: "flex-start",
  background: "rgba(20, 28, 24, 0.44)",
  display: "flex",
  inset: 0,
  justifyContent: "center",
  padding: "88px 20px 20px",
  position: "fixed",
  zIndex: 120,
};

const panelStyle: CSSProperties = {
  backdropFilter: "blur(18px)",
  background: "rgba(255, 251, 245, 0.96)",
  border: "1px solid rgba(52, 72, 63, 0.16)",
  borderRadius: "22px",
  boxShadow: "0 28px 80px rgba(22, 32, 25, 0.18)",
  display: "grid",
  gap: "14px",
  maxHeight: "min(72vh, 720px)",
  overflow: "hidden",
  padding: "18px",
  width: "min(720px, 100%)",
};

const triggerStyle: CSSProperties = {
  alignItems: "center",
  background: "rgba(255, 255, 255, 0.68)",
  border: "1px solid rgba(52, 72, 63, 0.12)",
  borderRadius: "999px",
  color: "#162019",
  cursor: "pointer",
  display: "inline-flex",
  font: "inherit",
  fontWeight: 700,
  gap: "10px",
  minHeight: "44px",
  padding: "0 14px",
};

const listStyle: CSSProperties = {
  display: "grid",
  gap: "8px",
  listStyle: "none",
  margin: 0,
  overflow: "auto",
  padding: 0,
};

const emptyStyle: CSSProperties = {
  border: "1px dashed rgba(52, 72, 63, 0.18)",
  borderRadius: "16px",
  color: "#4d5b52",
  margin: 0,
  padding: "18px 16px",
};

/**
 * Purpose: Render a searchable desktop command palette with Ctrl/Cmd+K support.
 * Inputs: Command items with labels, descriptions, destinations, and optional search keywords.
 * Outputs: A trigger button plus a modal command list when opened.
 * Behavior: Filters commands locally and closes automatically after the operator follows a command.
 */
export function CommandPalette({
  items,
  triggerLabel = "Command palette",
}: CommandPaletteProps): ReactElement {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setIsOpen(true);
      }

      if (event.key === "Escape") {
        setIsOpen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const timer = window.setTimeout(() => {
      inputRef.current?.focus();
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [isOpen]);

  const filteredItems = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (normalizedQuery.length === 0) {
      return items;
    }

    return items.filter((item) => {
      const haystack = [item.label, item.description, ...(item.keywords ?? [])]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalizedQuery);
    });
  }, [items, query]);

  const handleDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      setIsOpen(false);
    }
  };

  return (
    <>
      <button
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        onClick={() => setIsOpen(true)}
        style={triggerStyle}
        type="button"
      >
        <span>{triggerLabel}</span>
        <kbd
          style={{
            background: "rgba(22, 32, 25, 0.06)",
            borderRadius: "999px",
            fontFamily: "inherit",
            fontSize: "0.74rem",
            fontWeight: 700,
            padding: "4px 8px",
          }}
        >
          Ctrl/Cmd K
        </kbd>
      </button>

      {isOpen ? (
        <div
          aria-modal="true"
          onClick={() => setIsOpen(false)}
          onKeyDown={handleDialogKeyDown}
          role="dialog"
          style={overlayStyle}
        >
          <div onClick={(event) => event.stopPropagation()} style={panelStyle}>
            <div style={{ display: "grid", gap: "8px" }}>
              <label
                htmlFor="workspace-command-search"
                style={{
                  color: "#4d5b52",
                  fontSize: "0.82rem",
                  fontWeight: 700,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                }}
              >
                Jump to
              </label>
              <input
                id="workspace-command-search"
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search dashboard, entity workspaces, queues, and reviews"
                ref={inputRef}
                style={{
                  background: "rgba(255, 255, 255, 0.9)",
                  border: "1px solid rgba(52, 72, 63, 0.14)",
                  borderRadius: "16px",
                  color: "#162019",
                  font: "inherit",
                  padding: "14px 16px",
                }}
                type="search"
                value={query}
              />
            </div>

            {filteredItems.length === 0 ? (
              <p style={emptyStyle}>No commands match that search yet.</p>
            ) : (
              <ul style={listStyle}>
                {filteredItems.map((item) => (
                  <li key={item.id}>
                    <a
                      href={item.href}
                      onClick={() => setIsOpen(false)}
                      style={{
                        background: "rgba(255, 255, 255, 0.7)",
                        border: "1px solid rgba(52, 72, 63, 0.12)",
                        borderRadius: "16px",
                        color: "#162019",
                        display: "grid",
                        gap: "4px",
                        padding: "14px 16px",
                      }}
                    >
                      <strong style={{ fontSize: "1rem", letterSpacing: "-0.02em" }}>
                        {item.label}
                      </strong>
                      <span style={{ color: "#4d5b52", lineHeight: 1.55 }}>{item.description}</span>
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
