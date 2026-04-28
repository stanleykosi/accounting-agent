"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  useEffect,
  useMemo,
  useState,
  useTransition,
  type ReactElement,
  type ReactNode,
} from "react";
import {
  isSessionAuthError,
  logoutUser,
  readCurrentSession,
  readCurrentSessionSnapshot,
  type AuthSessionResponse,
} from "../../lib/auth/client";
import { readDashboardBootstrapSnapshot } from "../../lib/dashboard";
import {
  buildRememberedCloseContext,
  deriveRememberedCloseContextFromDashboardEntries,
  readRememberedCloseContext,
  subscribeRememberedCloseContext,
  type RememberedCloseContext,
  writeRememberedCloseContext,
} from "../../lib/workspace-navigation";
import { QuartzIcon, type QuartzIconName } from "./QuartzIcons";

type QuartzWorkspaceShellProps = Readonly<{
  children: ReactNode;
}>;

type NavItem = Readonly<{
  href: string;
  icon: QuartzIconName;
  isActive: boolean;
  label: string;
}>;

export function QuartzWorkspaceShell({ children }: QuartzWorkspaceShellProps): ReactElement {
  const pathname = usePathname();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [session, setSession] = useState<AuthSessionResponse | null>(() =>
    readCurrentSessionSnapshot(),
  );
  const [rememberedCloseContext, setRememberedCloseContext] =
    useState<RememberedCloseContext | null>(null);

  const closeContext = useMemo(() => resolveCloseContext(pathname), [pathname]);
  const fallbackCloseContext = useMemo(
    () => closeContext ?? rememberedCloseContext ?? resolveFallbackCloseContext(),
    [closeContext, rememberedCloseContext],
  );
  const entityContext = useMemo(() => resolveEntityContext(pathname), [pathname]);
  const breadcrumbs = useMemo(() => resolveBreadcrumbs(pathname), [pathname]);
  const isChatWorkspace = pathname.endsWith("/chat");
  const settingsHref = useMemo(
    () =>
      resolveSettingsHref({
        closeContext,
        entityContext,
      }),
    [closeContext, entityContext],
  );
  const isSettingsActive = pathname === "/settings" || pathname.endsWith("/settings");
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
        isActive:
          pathname.startsWith("/entities") &&
          !pathname.includes("/close-runs/") &&
          !pathname.endsWith("/assistant"),
        label: "Entities",
      },
      {
        href: fallbackCloseContext?.overviewHref ?? "/entities",
        icon: "close",
        isActive: pathname.includes("/close-runs/") && !pathname.endsWith("/chat"),
        label: "Close",
      },
      {
        href: "/assistant",
        icon: "assistant",
        isActive:
          pathname === "/assistant" ||
          pathname.endsWith("/assistant") ||
          pathname.endsWith("/chat"),
        label: "Assistant",
      },
    ],
    [fallbackCloseContext?.overviewHref, pathname],
  );
  const userIdentity = useMemo(
    () => ({
      email: session?.user.email ?? "Session syncing...",
      fullName: session?.user.full_name ?? "Authenticated Operator",
      initials: session ? buildOperatorInitials(session) : "AA",
    }),
    [session],
  );

  useEffect(() => {
    if (closeContext !== null) {
      const nextContext = buildRememberedCloseContext(
        closeContext.entityId,
        closeContext.closeRunId,
      );
      setRememberedCloseContext(nextContext);
      writeRememberedCloseContext(nextContext);
      return;
    }

    setRememberedCloseContext(readRememberedCloseContext() ?? resolveFallbackCloseContext());
  }, [closeContext, pathname]);

  useEffect(() => {
    return subscribeRememberedCloseContext((context) => {
      setRememberedCloseContext(context);
    });
  }, []);

  useEffect(() => {
    let isActive = true;

    void readCurrentSession()
      .then((nextSession) => {
        if (isActive) {
          setSession(nextSession);
        }
      })
      .catch((error: unknown) => {
        if (!isActive || isSessionAuthError(error)) {
          return;
        }

        setSession((currentSession) => currentSession);
      });

    return () => {
      isActive = false;
    };
  }, []);

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
          <Link
            aria-current={isSettingsActive ? "page" : undefined}
            className={isSettingsActive ? "quartz-nav-link active" : "quartz-nav-link"}
            href={settingsHref}
            title="Settings"
          >
            <QuartzIcon className="quartz-nav-icon" name="settings" />
            <span>Settings</span>
          </Link>
          <button
            className="quartz-nav-button"
            disabled={isPending}
            onClick={handleLogout}
            title="Sign out"
            type="button"
          >
            <QuartzIcon className="quartz-nav-icon" name="logout" />
            <span>{isPending ? "Signing out" : "Sign out"}</span>
          </button>
        </div>
      </aside>

      <div
        className={
          isChatWorkspace ? "quartz-shell-main quartz-shell-main-chat" : "quartz-shell-main"
        }
      >
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
            <div className="quartz-user-pill">
              <span className="quartz-user-initials">{userIdentity.initials}</span>
              <div>
                <strong>{userIdentity.fullName}</strong>
                <span>{userIdentity.email}</span>
              </div>
            </div>
          </div>
        </header>

        <div
          className={
            isChatWorkspace
              ? "quartz-shell-content quartz-shell-content-chat"
              : "quartz-shell-content"
          }
        >
          {children}
        </div>
      </div>
    </div>
  );
}

function buildOperatorInitials(session: Readonly<AuthSessionResponse>): string {
  const nameParts = session.user.full_name
    .split(/\s+/u)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  const initials = nameParts
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

  if (initials.length > 0) {
    return initials;
  }

  return session.user.email.slice(0, 2).toUpperCase();
}

function resolveCloseContext(pathname: string): {
  chatHref: string;
  closeRunId: string;
  entityId: string;
  overviewHref: string;
} | null {
  const match = pathname.match(/^\/entities\/([^/]+)\/close-runs\/([^/]+)/u);
  if (match === null) {
    return null;
  }

  const entityId = match[1];
  const closeRunId = match[2];
  if (typeof entityId !== "string" || typeof closeRunId !== "string") {
    return null;
  }

  return {
    chatHref: `/entities/${entityId}/close-runs/${closeRunId}/chat`,
    closeRunId,
    entityId,
    overviewHref: `/entities/${entityId}/close-runs/${closeRunId}`,
  };
}

function resolveEntityContext(pathname: string): {
  assistantHref: string;
  entityId: string;
  homeHref: string;
} | null {
  const match = pathname.match(/^\/entities\/([^/]+)(?:\/.*)?$/u);
  if (match === null) {
    return null;
  }

  const entityId = match[1];
  if (typeof entityId !== "string" || entityId.length === 0 || entityId === "new") {
    return null;
  }
  if (pathname.includes("/close-runs/")) {
    return null;
  }

  return {
    assistantHref: `/entities/${entityId}/assistant`,
    entityId,
    homeHref: `/entities/${entityId}`,
  };
}

function resolveSettingsHref(options: {
  closeContext: {
    chatHref: string;
    closeRunId: string;
    entityId: string;
    overviewHref: string;
  } | null;
  entityContext: {
    assistantHref: string;
    entityId: string;
    homeHref: string;
  } | null;
}): string {
  if (options.closeContext !== null) {
    return `/entities/${options.closeContext.entityId}/settings`;
  }

  if (options.entityContext !== null) {
    return `/entities/${options.entityContext.entityId}/settings`;
  }

  return "/settings";
}

function resolveFallbackCloseContext(): RememberedCloseContext | null {
  const dashboardSnapshot = readDashboardBootstrapSnapshot();
  return dashboardSnapshot === null
    ? null
    : deriveRememberedCloseContextFromDashboardEntries(dashboardSnapshot);
}

function resolveBreadcrumbs(pathname: string): readonly { href: string; label: string }[] {
  if (pathname === "/settings") {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/settings", label: "Settings" },
    ];
  }

  if (pathname === "/assistant") {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/assistant", label: "Global Assistant" },
    ];
  }

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

  if (/^\/entities\/[^/]+\/assistant$/u.test(pathname)) {
    const entityHomePath = pathname.replace(/\/assistant$/u, "");
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: entityHomePath, label: "Entity Home" },
      { href: pathname, label: "Entity Assistant" },
    ];
  }

  if (/^\/entities\/[^/]+\/settings$/u.test(pathname)) {
    const entityHomePath = pathname.replace(/\/settings$/u, "");
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: entityHomePath, label: "Entity Home" },
      { href: pathname, label: "Workspace Settings" },
    ];
  }

  if (pathname.endsWith("/chat")) {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: pathname.replace(/\/chat$/u, ""), label: "Close Run Control" },
      { href: pathname, label: "Close Assistant" },
    ];
  }

  const closeRunWorkspaceLabel = resolveCloseRunWorkspaceLabel(pathname);
  if (closeRunWorkspaceLabel !== null) {
    const closeRunRootPath = pathname.replace(
      /\/(documents|recommendations|reconciliation|reports|exports|complete)$/u,
      "",
    );
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: closeRunRootPath, label: "Close Run Control" },
      { href: pathname, label: closeRunWorkspaceLabel },
    ];
  }

  const entityWorkspaceLabel = resolveEntityWorkspaceLabel(pathname);
  if (entityWorkspaceLabel !== null) {
    const entityHomePath = pathname
      .replace(/\/reports\/templates$/u, "")
      .replace(/\/(coa|ledger|integrations)$/u, "");
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: entityHomePath, label: "Entity Home" },
      { href: pathname, label: entityWorkspaceLabel },
    ];
  }

  if (pathname.includes("/close-runs/")) {
    return [
      { href: "/", label: "Portfolio Overview" },
      { href: "/entities", label: "Entities" },
      { href: pathname, label: "Close Run Control" },
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

function resolveEntityWorkspaceLabel(pathname: string): string | null {
  if (/\/entities\/[^/]+\/coa$/u.test(pathname)) {
    return "Chart of Accounts";
  }

  if (/\/entities\/[^/]+\/ledger$/u.test(pathname)) {
    return "Imported Ledger";
  }

  if (/\/entities\/[^/]+\/integrations$/u.test(pathname)) {
    return "Integrations";
  }

  if (/\/entities\/[^/]+\/reports\/templates$/u.test(pathname)) {
    return "Report Templates";
  }

  return null;
}

function resolveCloseRunWorkspaceLabel(pathname: string): string | null {
  if (/\/close-runs\/[^/]+\/documents$/u.test(pathname)) {
    return "Document Workspace";
  }

  if (/\/close-runs\/[^/]+\/recommendations$/u.test(pathname)) {
    return "Recommendations & Journals";
  }

  if (/\/close-runs\/[^/]+\/reconciliation$/u.test(pathname)) {
    return "Reconciliation";
  }

  if (/\/close-runs\/[^/]+\/reports$/u.test(pathname)) {
    return "Reporting & Commentary";
  }

  if (/\/close-runs\/[^/]+\/exports$/u.test(pathname)) {
    return "Sign-Off & Release";
  }

  if (/\/close-runs\/[^/]+\/complete$/u.test(pathname)) {
    return "Close Complete";
  }

  return null;
}
