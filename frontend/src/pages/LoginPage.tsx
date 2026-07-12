import { useEffect, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { bootstrapStatus } from "../api/auth";
import { ApiError } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { AlertIcon, ShieldIcon } from "../components/icons";
import { Spinner } from "../components/Spinner";
import { ROUTES } from "../routes";

interface LocationState {
  readonly from?: string;
}

/** Where the bootstrap status check has settled. */
type Mode = "checking" | "login" | "bootstrap";

/** Operator-facing summary of the backend password policy. */
const PASSWORD_POLICY =
  "Use at least 12 characters with upper- and lower-case letters, a digit, and a symbol.";

const BOOTSTRAP_STEPS = [
  "Create administrator",
  "Connect provider account",
  "Sync available models",
  "Build fallback chain",
  "Issue and test API key",
] as const;

/**
 * Public auth entry point.
 *
 * On mount it asks the backend whether first-run bootstrap is still required
 * (GET /api/auth/bootstrap). While checking it shows a loading state; if bootstrap
 * is required it shows the "Create the first administrator" form; otherwise it
 * shows the normal login form. If the status check itself fails, it falls back to
 * the login form so an existing install is never locked out.
 *
 * Both forms demonstrate the required async states: idle, submitting (controls
 * disabled), and a secret-free error message from the API envelope. On success the
 * session is stored and the operator is sent to the location they requested, or the
 * dashboard.
 */
export function LoginPage(): JSX.Element {
  const [mode, setMode] = useState<Mode>("checking");
  const [checkFailed, setCheckFailed] = useState(false);

  useEffect(() => {
    let active = true;
    bootstrapStatus()
      .then((status) => {
        if (active) {
          setMode(status.bootstrap_required ? "bootstrap" : "login");
        }
      })
      .catch(() => {
        // Fail safe to the login form: a fresh install returns a definitive
        // bootstrap_required flag, so an unreachable check most likely means an
        // existing setup or a transient network issue.
        if (active) {
          setCheckFailed(true);
          setMode("login");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <main className="auth">
      <section className="auth__card" aria-labelledby="auth-title">
        <div className="auth__brand">
          <ShieldIcon size={28} aria-hidden />
          <h1 id="auth-title">Gozar</h1>
        </div>

        {mode === "checking" ? (
          <div className="auth__checking" role="status">
            <Spinner label="Checking setup" size={24} />
            <span>Checking setup...</span>
          </div>
        ) : mode === "bootstrap" ? (
          <BootstrapForm />
        ) : (
          <LoginForm checkFailed={checkFailed} />
        )}
      </section>
    </main>
  );
}

/** The standard sign-in form shown once bootstrap is no longer required. */
function LoginForm({ checkFailed }: { readonly checkFailed: boolean }): JSX.Element {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signIn({ username, password });
      const state = location.state as LocationState | null;
      navigate(state?.from ?? ROUTES.dashboard, { replace: true });
    } catch (cause) {
      if (cause instanceof ApiError) {
        setError(cause.isAuthError ? "Invalid username or password." : cause.message);
      } else {
        setError("Unexpected error. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <p className="auth__subtitle">
        Sign in to manage accounts, tokens, and routing.
      </p>

      {checkFailed && (
        <p className="alert alert--warn page-alert" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>Could not verify setup status. You can still sign in below.</span>
        </p>
      )}

      <form className="auth__form" onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor="username">Username</label>
          <input
            id="username"
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />
        </div>

        {error !== null && (
          <p className="alert alert--error" role="alert">
            <AlertIcon size={18} aria-hidden />
            <span>{error}</span>
          </p>
        )}

        <button
          type="submit"
          className="button button--primary"
          disabled={submitting || username.length === 0 || password.length === 0}
        >
          {submitting ? (
            <>
              <Spinner label="Signing in" size={18} />
              <span>Signing in...</span>
            </>
          ) : (
            <span>Sign in</span>
          )}
        </button>
      </form>
    </>
  );
}

/** First-run form to create the initial administrator account. */
function BootstrapForm(): JSX.Element {
  const { completeBootstrap } = useAuth();
  const navigate = useNavigate();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = confirm.length > 0 && password !== confirm;
  const canSubmit =
    !submitting &&
    username.trim().length > 0 &&
    password.length > 0 &&
    password === confirm;

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (password !== confirm) {
      setError("The passwords do not match.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await completeBootstrap({ username: username.trim(), password });
      navigate(ROUTES.dashboard, { replace: true });
    } catch (cause) {
      if (cause instanceof ApiError) {
        setError(cause.message);
      } else {
        setError("Unexpected error. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <p className="auth__subtitle">
        Welcome to Gozar. Create the first administrator to get started.
      </p>

      <ol className="auth-roadmap" aria-label="Gozar setup steps">
        {BOOTSTRAP_STEPS.map((step, index) => (
          <li key={step} className={index === 0 ? "auth-roadmap__step auth-roadmap__step--active" : "auth-roadmap__step"}>
            <span>{index + 1}</span>
            <strong>{step}</strong>
          </li>
        ))}
      </ol>

      <form className="auth__form" onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor="bootstrap-username">Username</label>
          <input
            id="bootstrap-username"
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
          />
        </div>

        <div className="field">
          <label htmlFor="bootstrap-password">Password</label>
          <input
            id="bootstrap-password"
            name="new-password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
            aria-describedby="bootstrap-policy"
          />
        </div>

        <div className="field">
          <label htmlFor="bootstrap-confirm">Confirm password</label>
          <input
            id="bootstrap-confirm"
            name="confirm-password"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={submitting}
            aria-invalid={mismatch}
          />
        </div>

        <p id="bootstrap-policy" className="auth__policy">
          {PASSWORD_POLICY}
        </p>

        {mismatch && (
          <p className="alert alert--warn" role="alert">
            <AlertIcon size={18} aria-hidden />
            <span>The passwords do not match.</span>
          </p>
        )}

        {error !== null && (
          <p className="alert alert--error" role="alert">
            <AlertIcon size={18} aria-hidden />
            <span>{error}</span>
          </p>
        )}

        <button type="submit" className="button button--primary" disabled={!canSubmit}>
          {submitting ? (
            <>
              <Spinner label="Creating administrator" size={18} />
              <span>Creating administrator...</span>
            </>
          ) : (
            <span>Create administrator</span>
          )}
        </button>
      </form>
    </>
  );
}
