"use client";

import type { ReactElement, SVGProps } from "react";

export type QuartzIconName =
  | "assistant"
  | "bell"
  | "check"
  | "close"
  | "dismiss"
  | "entities"
  | "filter"
  | "folder"
  | "logout"
  | "help"
  | "portfolio"
  | "refresh"
  | "search"
  | "send"
  | "settings"
  | "sparkle"
  | "trash"
  | "trendDown"
  | "trendUp"
  | "upload"
  | "warning";

type QuartzIconProps = SVGProps<SVGSVGElement> & {
  name: QuartzIconName;
};

export function QuartzIcon({ name, ...props }: Readonly<QuartzIconProps>): ReactElement {
  switch (name) {
    case "assistant":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M12 3l1.65 4.35L18 9l-4.35 1.65L12 15l-1.65-4.35L6 9l4.35-1.65L12 3zM19 14l.9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9L19 14zM5 15l.75 1.75L7.5 17.5l-1.75.75L5 20l-.75-1.75L2.5 17.5l1.75-.75L5 15z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "bell":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M15 18a3 3 0 01-6 0m8.5-2.5H6.5l1.25-1.5V10a4.25 4.25 0 018.5 0v4l1.25 1.5z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "check":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M20 6L9 17l-5-5"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "close":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M9 4h6l5 5v10a1 1 0 01-1 1H5a1 1 0 01-1-1V9l5-5z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
          <path
            d="M9 12l2 2 4-4"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "entities":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M4 20V6l4-2 4 2v14M8 9h0M8 13h0M8 17h0M14 20V10l3-1.5L20 10v10M17 13h0M17 17h0"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "dismiss":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M6 6l12 12M18 6L6 18"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "filter":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M4 6h16M7 12h10M10 18h4"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "folder":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M3 7.5A1.5 1.5 0 014.5 6H9l1.6 2H19.5A1.5 1.5 0 0121 9.5v7A1.5 1.5 0 0119.5 18h-15A1.5 1.5 0 013 16.5v-9z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "logout":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M10 5H6a2 2 0 00-2 2v10a2 2 0 002 2h4M14 16l4-4-4-4M18 12H9"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "help":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M9.5 9a2.5 2.5 0 115 0c0 2-2.5 2-2.5 4m0 3h.01M22 12a10 10 0 11-20 0 10 10 0 0120 0z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "portfolio":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M4 7h16M7 4h10v16H7zM4 20h16"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "refresh":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M20 6v5h-5M4 18v-5h5"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
          <path
            d="M18 11a7 7 0 00-12-2M6 13a7 7 0 0012 2"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "search":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M10.5 18a7.5 7.5 0 100-15 7.5 7.5 0 000 15zM16 16l5 5"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "send":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M21 3L10 14M21 3l-7 18-4-7-7-4 18-7z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "settings":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M12 8.5A3.5 3.5 0 1012 15.5 3.5 3.5 0 0012 8.5zm8 3.5l-2 .75c-.1.37-.24.72-.42 1.05l.86 1.97-1.92 1.92-1.97-.86c-.33.18-.68.32-1.05.42L12 20l-1.5-2.75c-.37-.1-.72-.24-1.05-.42l-1.97.86-1.92-1.92.86-1.97A5.7 5.7 0 016 12l-2-.75v-2.5l2-.75c.1-.37.24-.72.42-1.05l-.86-1.97 1.92-1.92 1.97.86c.33-.18.68-.32 1.05-.42L12 1l1.5 2.75c.37.1.72.24 1.05.42l1.97-.86 1.92 1.92-.86 1.97c.18.33.32.68.42 1.05l2 .75V12z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.4"
          />
        </svg>
      );
    case "sparkle":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8L12 2z"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "trash":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M5 7h14M9 7V5h6v2M8 10v7M12 10v7M16 10v7M7 7l1 12h8l1-12"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "trendDown":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M4 7l6 6 4-4 6 6M20 11v4h-4"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "trendUp":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M4 17l6-6 4 4 6-6M16 5h4v4"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "upload":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M12 16V5M12 5l-4 4M12 5l4 4M5 19h14"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
    case "warning":
      return (
        <svg fill="none" viewBox="0 0 24 24" {...props}>
          <path
            d="M12 4l9 16H3L12 4zm0 5v5m0 3h.01"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      );
  }
}
