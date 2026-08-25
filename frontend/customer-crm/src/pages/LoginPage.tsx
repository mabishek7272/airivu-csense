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

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
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
      if (err instanceof ApiRequestError) {
        setError(err.body.message);
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main style={{ maxWidth: 380, margin: "10vh auto", fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ fontSize: 20 }}>AIRIVU CSense</h1>
      <p style={{ color: "#555", marginBottom: 24 }}>Customer CRM — {mode === "login" ? "sign in" : "create your organization"}</p>

      <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {mode === "register" && (
          <>
            <label>
              Organization name
              <input value={organizationName} onChange={(e) => setOrganizationName(e.target.value)} required />
            </label>
            <label>
              Your name
              <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
            </label>
          </>
        )}
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={12}
            required
          />
        </label>

        {error && (
          <p role="alert" style={{ color: "#b00020" }}>
            {error}
          </p>
        )}

        <button type="submit" disabled={submitting}>
          {submitting ? "Please wait…" : mode === "login" ? "Sign in" : "Create organization"}
        </button>
      </form>

      <button
        type="button"
        onClick={() => setMode(mode === "login" ? "register" : "login")}
        style={{ marginTop: 16, background: "none", border: "none", color: "#0060df", cursor: "pointer" }}
      >
        {mode === "login" ? "Need an account? Register your organization" : "Already have an account? Sign in"}
      </button>
    </main>
  );
}
