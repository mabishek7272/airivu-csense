import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

/** The one page in this app reachable without already being signed in - same shell as
 *  LoginPage, reached from the link an invitation email carries (`?token=...`). Accepting
 *  logs the person straight in, the same as a fresh registration does.
 */
export function AcceptInvitationPage() {
  const { acceptInvitation } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    setSubmitting(true);
    try {
      await acceptInvitation(token, password);
      navigate("/incidents");
    } catch (err) {
      setError(
        err instanceof ApiRequestError
          ? err.body.message
          : "Something went wrong. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (!token) {
    return (
      <main className="auth-shell">
        <div className="card auth-card">
          <h1>AIRIVU CSense</h1>
          <p role="alert">This invitation link is missing its token. Check the link in your email.</p>
        </div>
      </main>
    );
  }

  return (
    <main className="auth-shell">
      <div className="card auth-card">
        <h1>AIRIVU CSense</h1>
        <p>Set a password to accept your invitation</p>

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={12}
              required
              autoComplete="new-password"
              aria-describedby="password-hint"
            />
            <small id="password-hint" style={{ color: "var(--text-muted)" }}>
              At least 12 characters.
            </small>
          </div>

          <div className="field">
            <label htmlFor="confirm-password">Confirm password</label>
            <input
              id="confirm-password"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              minLength={12}
              required
              autoComplete="new-password"
            />
          </div>

          {error && (
            <p role="alert" style={{ color: "var(--critical)" }}>
              {error}
            </p>
          )}

          <button type="submit" className="primary" disabled={submitting} style={{ width: "100%" }}>
            {submitting ? "Please wait…" : "Accept invitation"}
          </button>
        </form>
      </div>
    </main>
  );
}
