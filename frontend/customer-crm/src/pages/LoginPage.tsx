import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      if (mode === "login") {
        await login(email, password);
      } else {
        await register(organizationName, email, password, displayName);
      }
      navigate("/dashboard");
    } catch (err) {
      setError(
        err instanceof ApiRequestError ? err.body.message : "Something went wrong. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <div className="card auth-card">
        <h1>AIRIVU CSense</h1>
        <p>{mode === "login" ? "Sign in to your workspace" : "Create your organization"}</p>

        <form onSubmit={handleSubmit}>
          {mode === "register" && (
            <>
              <div className="field">
                <label htmlFor="org">Organization name</label>
                <input
                  id="org"
                  value={organizationName}
                  onChange={(e) => setOrganizationName(e.target.value)}
                  required
                  autoComplete="organization"
                />
              </div>
              <div className="field">
                <label htmlFor="name">Your name</label>
                <input
                  id="name"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  required
                  autoComplete="name"
                />
              </div>
            </>
          )}

          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={12}
              required
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              aria-describedby={mode === "register" ? "password-hint" : undefined}
            />
            {mode === "register" && (
              <small id="password-hint" style={{ color: "var(--text-muted)" }}>
                At least 12 characters.
              </small>
            )}
          </div>

          {error && (
            <p role="alert" style={{ color: "var(--critical)" }}>
              {error}
            </p>
          )}

          <button type="submit" className="primary" disabled={submitting} style={{ width: "100%" }}>
            {submitting ? "Please wait…" : mode === "login" ? "Sign in" : "Create organization"}
          </button>
        </form>

        <button
          type="button"
          className="link"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
          style={{ marginTop: 16 }}
        >
          {mode === "login"
            ? "Need an account? Register your organization"
            : "Already have an account? Sign in"}
        </button>
      </div>
    </main>
  );
}
