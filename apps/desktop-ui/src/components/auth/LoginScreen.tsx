/*
Purpose: Render the interactive login and registration form used by the auth entry page.
Scope: Client-side form state, same-origin auth submissions, and operator-facing auth feedback.
Dependencies: Next.js client navigation and desktop auth helpers.
*/

"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition, type ChangeEvent, type FormEvent, type ReactElement } from "react";
import { QuartzIcon } from "../layout/QuartzIcons";
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
  const title = mode === "login" ? "Sign in" : "Sign up";
  const submitLabel = mode === "login" ? "Sign in" : "Sign up";
  const helperCopy =
    mode === "login"
      ? "Sign in to continue to your accounting workspace."
      : "Create your account to access the accounting workspace.";

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
    <main className="quartz-auth-shell">
      <section className="quartz-auth-card">
        <div className="quartz-auth-card-body">
          <div className="quartz-auth-brand">
            <QuartzIcon className="quartz-auth-icon" name="entities" />
            <span>Accounting AI Agent</span>
          </div>

          <header>
            <h1 className="quartz-auth-title">{title}</h1>
            <p className="quartz-auth-copy">{helperCopy}</p>
          </header>

          {noticeMessage ? (
            <div className="status-banner warning" role="status">
              {noticeMessage}
            </div>
          ) : null}

          <form className="quartz-auth-form" onSubmit={handleSubmit}>
            <label className="quartz-form-label">
              <span>Email Address</span>
              <input
                autoComplete="email"
                className="text-input"
                name="email"
                onChange={handleFieldChange("email")}
                placeholder="controller@apexmeridian.ng"
                required
                type="email"
                value={formState.email}
              />
            </label>

            {mode === "register" ? (
              <label className="quartz-form-label">
                <span>Full Name</span>
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

            <label className="quartz-form-label">
              <div className="quartz-form-row">
                <span>Password</span>
                {mode === "login" ? (
                  <button
                    className="quartz-form-link"
                    onClick={() => setFeedbackMessage("Password reset is handled by the local administrator.")}
                    type="button"
                  >
                    Reset Access
                  </button>
                ) : null}
              </div>
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

            <button
              className="quartz-form-link"
              onClick={() => {
                setMode((currentMode) => (currentMode === "login" ? "register" : "login"));
                setFeedbackMessage(null);
              }}
              type="button"
            >
              {mode === "login" ? "Sign up" : "Back to sign in"}
            </button>
          </form>
        </div>
      </section>
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
      return "Your previous session expired while you were away. Sign in again to resume the current close workspace.";
    case "user-disabled":
      return "This operator account is disabled. Use another local account or reactivate it from the administration surface.";
    case "auth-required":
      return "Sign in to continue into the protected accounting workspace.";
    default:
      return undefined;
  }
}

function resolveAuthMessage(error: unknown): string {
  if (isAuthApiError(error)) {
    return error.message;
  }

  return "Authentication is temporarily unavailable. Reload the workspace and try again.";
}
