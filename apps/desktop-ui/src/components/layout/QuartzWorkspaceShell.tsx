"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useMemo, useTransition, type ReactElement, type ReactNode } from "react";
import { logoutUser } from "../../lib/auth/client";
import { QuartzIcon, type QuartzIconName } from "./QuartzIcons";

type QuartzWorkspaceShellProps = Readonly<{
  children: ReactNode;
  userEmail: string;
  userFullName: string;
  userInitials: string;
}>;

type NavItem = Readonly<{
  href: string;
  icon: QuartzIconName;
  isActive: boolean;
  label: string;
}>;

export function QuartzWorkspaceShell({
  children,
  userEmail,
  userFullName,
  userInitials,
}: QuartzWorkspaceShellProps): ReactElement {
  const pathname = usePathname();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  const closeContext = useMemo(() => resolveCloseContext(pathname), [pathname]);
  const breadcrumbs = useMemo(() => resolveBreadcrumbs(pathname), [pathname]);
  const workspaceAction = useMemo(
    () => resolveWorkspaceAction(pathname, closeContext),
    [closeContext, pathname],
  );
  const navItems = useMemo<readonly NavItem[]>(
    () => [
      {
        href: "/",
        icon: "portfolio",
        isActive: pathname === "/",
        label: "Portfolio",
      },
      {
        href: "/entities",
        icon: "entities",
        isActive: pathname.startsWith("/entities") && !pathname.includes("/close-runs/"),
        label: "Entities",
      },
      {
        href: closeContext?.overviewHref ?? "/",
        icon: "close",
        isActive: pathname.includes("/close-runs/") && !pathname.endsWith("/chat"),
        label: "Close",
      },
      {
        href: closeContext?.chatHref ?? "/",
        icon: "assistant",
        isActive: pathname.endsWith("/chat"),
        label: "Assistant",
      },
    ],
    [closeContext?.chatHref, closeContext?.overviewHref, pathname],
  );

  const handleLogout = (): void => {
    startTransition(async () => {
      try {
        await logoutUser();
      } catch {
        // Returning the operator to login is still the correct recovery path.
      } finally {
        router.replace("/login");
        router.refresh();
      }
    });
  };

  return (
    <div className="quartz-shell">
      <aside className="quartz-sidebar">
        <Link className="quartz-sidebar-brand" href="/" title="Accounting AI Agent">
          <span>AA</span>
        </Link>

        <nav className="quartz-sidebar-nav" aria-label="Primary workspace navigation">
          {navItems.map((item) => (
            <Link
              aria-current={item.isActive ? "page" : undefined}
              className={item.isActive ? "quartz-nav-link active" : "quartz-nav-link"}
              href={item.href}
              key={item.label}
              title={item.label}
            >
              <QuartzIcon className="quartz-nav-icon" name={item.icon} />
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="quartz-sidebar-footer">
          <Link className="quartz-nav-link" href="/setup" title="Runtime setup">
            <QuartzIcon className="quartz-nav-icon" name="settings" />
            <span>Setup</span>
          </Link>
        </div>
      </aside>

      <div className="quartz-shell-main">
        <header className="quartz-topbar">
          <div className="quartz-topbar-left">
            <span className="quartz-topbar-brand">Accounting AI Agent</span>
            <nav className="quartz-breadcrumbs" aria-label="Workspace context">
              {breadcrumbs.map((item, index) => {
                const isLast = index === breadcrumbs.length - 1;
                return isLast ? (
                  <span className="quartz-breadcrumb active" key={item.label}>
                    {item.label}
                  </span>
                ) : (
                  <Link className="quartz-breadcrumb" href={item.href} key={item.label}>
                    {item.label}
                  </Link>
                );
              })}
            </nav>
          </div>

          <div className="quartz-topbar-right">
            <Link className="quartz-toolbar-button" href={workspaceAction.href}>
              {workspaceAction.label}
            </Link>
            <button
              className="quartz-toolbar-button primary"
              disabled={isPending}
              onClick={handleLogout}
              type="button"
            >
              {isPending ? "Signing out..." : "Sign out"}
            </button>
            <div className="quartz-topbar-icons" aria-hidden="true">
              <QuartzIcon className="quartz-topbar-icon" name="bell" />
              <QuartzIcon className="quartz-topbar-icon" name="help" />
            </div>
            <div className="quartz-user-pill">
              <span className="quartz-user-initials">{userInitials}</span>
              <div>
                <strong>{userFullName}</strong>
                <span>{userEmail}</span>
              </div>
            </div>
          </div>
        </header>

        <div className="quartz-shell-content">{children}</div>
      </div>
    </div>
  );
}

function resolveCloseContext(pathname: string): {
  chatHref: string;
  overviewHref: string;
} | null {
  const match = pathname.match(/^\/entities\/([^/]+)\/close-runs\/([^/]+)/u);
  if (match === null) {
    return null;
  }

  const entityId = match[1];
  const closeRunId = match[2];
  return {
    chatHref: `/entities/${entityId}/close-runs/${closeRunId}/chat`,
    overviewHref: `/entities/${entityId}/close-runs/${closeRunId}`,
  };
}

function resolveBreadcrumbs(pathname: string): readonly { href: string; label: string }[] {
  if (pathname === "/") {
    return [{ href: "/", label: "Portfolio Overview" }];
  }

  if (pathname === "/entities") {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
    ];
  }

  if (pathname === "/entities/new") {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: "/entities/new", label: "New Workspace" },
    ];
  }

  if (pathname.endsWith("/chat")) {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: pathname.replace(/\/chat$/u, ""), label: "Close Mission Control" },
      { href: pathname, label: "Assistant" },
    ];
  }

  if (pathname.includes("/close-runs/")) {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: pathname, label: "Close Mission Control" },
    ];
  }

  if (pathname.startsWith("/entities/")) {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: pathname, label: "Entity Home" },
    ];
  }

  return [{ href: "/", label: "Portfolio Overview" }];
}

function resolveWorkspaceAction(
  pathname: string,
  closeContext: ReturnType<typeof resolveCloseContext>,
): {
  href: string;
  label: string;
} {
  if (closeContext) {
    return {
      href: closeContext.chatHref,
      label: "Open Assistant",
    };
  }

  if (pathname === "/entities/new") {
    return {
      href: "/entities",
      label: "Entity Directory",
    };
  }

  return {
    href: "/entities/new",
    label: "Create Workspace",
  };
}
