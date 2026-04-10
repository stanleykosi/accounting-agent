/*
Purpose: Render the interactive login and registration form used by the auth entry page.
Scope: Client-side form state, same-origin auth submissions, and operator-facing auth feedback.
Dependencies: Next.js client navigation, the shared AuthGate UI, and desktop auth helpers.
*/

"use client";

import { AuthGate } from "@accounting-ai-agent/ui";
import { useRouter } from "next/navigation";
import { useState, useTransition, type ChangeEvent, type FormEvent, type ReactElement } from "react";
import { isAuthApiError, loginUser, registerUser } from "../../lib/auth/client";
import { resolvePostLoginPath } from "../../lib/auth/session";

type AuthMode = "login" | "register";

type AuthFormState = {
  email: string;
  fullName: string;
  password: string;
};

type LoginScreenProps = {
  initialNextPath: string | null;
  initialReason: string | null;
};

const defaultFormState: AuthFormState = {
  email: "",
  fullName: "",
  password: "",
};

const workflowHighlights = [
  "One local sign-in controls desktop review, approvals, and audit attribution.",
  "Session expiry routes back through the same login flow with the return path preserved.",
  "No secondary browser auth path exists: the same FastAPI local-auth contract backs every session.",
];

/**
 * Purpose: Provide the interactive auth entry surface for the desktop workspace.
 * Inputs: The preserved post-login path and any session-recovery reason from middleware.
 * Outputs: A client-rendered login or registration form that authenticates through the auth proxy.
 * Behavior: Supports both sign-in and first-user registration without introducing a separate auth route.
 */
export function LoginScreen({
  initialNextPath,
  initialReason,
}: Readonly<LoginScreenProps>): ReactElement {
  const router = useRouter();
  const [mode, setMode] = useState<AuthMode>("login");
  const [formState, setFormState] = useState<AuthFormState>(defaultFormState);
  const [feedbackMessage, setFeedbackMessage] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const noticeMessage = resolveNoticeMessage(initialReason);
  const submitLabel = mode === "login" ? "Sign in" : "Create account";
  const helperCopy =
    mode === "login"
      ? "Use the local operator account you created for this demo environment."
      : "Create the first local operator account for this workstation or add another reviewer.";

  const handleFieldChange =
    (fieldName: keyof AuthFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      setFormState((currentState) => ({
        ...currentState,
        [fieldName]: event.target.value,
      }));
    };

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setFeedbackMessage(null);

    startTransition(() => {
      void submitAuthForm({
        formState,
        mode,
      })
        .then(() => {
          router.replace(resolvePostLoginPath(initialNextPath));
          router.refresh();
        })
        .catch((error: unknown) => {
          setFeedbackMessage(resolveAuthMessage(error));
        });
    });
  };

  return (
    <main className="auth-shell">
      <div className="auth-grid">
        <AuthGate
          description="Sign in to the canonical desktop workspace for close runs, review queues, and evidence-backed accounting decisions."
          noticeTone={noticeMessage ? "warning" : "default"}
          supportingContent={
            <div className="detail-block">
              <h2>Why this flow exists</h2>
              <ul className="detail-list">
                {workflowHighlights.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          }
          title="Enter the local accounting workspace."
          {...(noticeMessage ? { notice: noticeMessage } : {})}
        >
          <div aria-label="Authentication mode" className="mode-toggle" role="tablist">
            <button
              aria-selected={mode === "login"}
              className={mode === "login" ? "mode-toggle-button active" : "mode-toggle-button"}
              onClick={() => setMode("login")}
              role="tab"
              type="button"
            >
              Sign in
            </button>
            <button
              aria-selected={mode === "register"}
              className={mode === "register" ? "mode-toggle-button active" : "mode-toggle-button"}
              onClick={() => setMode("register")}
              role="tab"
              type="button"
            >
              Create account
            </button>
          </div>

          <form className="auth-form" onSubmit={handleSubmit}>
            <p className="form-helper">{helperCopy}</p>

            <label className="field">
              <span>Email address</span>
              <input
                autoComplete="email"
                className="text-input"
                name="email"
                onChange={handleFieldChange("email")}
                placeholder="finance@example.com"
                required
                type="email"
                value={formState.email}
              />
            </label>

            {mode === "register" ? (
              <label className="field">
                <span>Full name</span>
                <input
                  autoComplete="name"
                  className="text-input"
                  name="fullName"
                  onChange={handleFieldChange("fullName")}
                  placeholder="Amina Okafor"
                  required
                  type="text"
                  value={formState.fullName}
                />
              </label>
            ) : null}

            <label className="field">
              <span>Password</span>
              <input
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                className="text-input"
                minLength={12}
                name="password"
                onChange={handleFieldChange("password")}
                placeholder="At least 12 characters"
                required
                type="password"
                value={formState.password}
              />
            </label>

            {feedbackMessage ? (
              <div className="status-banner danger" role="alert">
                {feedbackMessage}
              </div>
            ) : null}

            <button className="primary-button" disabled={isPending} type="submit">
              {isPending ? `${submitLabel}...` : submitLabel}
            </button>
          </form>
        </AuthGate>
      </div>
    </main>
  );
}

async function submitAuthForm(options: {
  formState: AuthFormState;
  mode: AuthMode;
}): Promise<void> {
  if (options.mode === "login") {
    await loginUser({
      email: options.formState.email,
      password: options.formState.password,
    });
    return;
  }

  await registerUser({
    email: options.formState.email,
    full_name: options.formState.fullName,
    password: options.formState.password,
  });
}

function resolveNoticeMessage(reason: string | null): string | undefined {
  switch (reason) {
    case "session-expired":
      return "Your previous session expired while you were away. Sign in again to resume where you left off.";
    case "user-disabled":
      return "This operator account is disabled. Use another local account or reactivate it from the admin surface.";
    case "auth-required":
      return "Sign in to continue to the protected accounting workspace.";
    default:
      return undefined;
  }
}

function resolveAuthMessage(error: unknown): string {
  if (isAuthApiError(error)) {
    return error.message;
  }

  return "Authentication is temporarily unavailable. Reload the desktop workspace and try again.";
}
