"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ApiRequestError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Something went wrong. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-shell">
      <div className="card auth-card">
        <h1>AIRIVU CSense</h1>
        <p>Developer Console — platform operator sign-in</p>

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label>
            Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          </label>
          <label>
            Password
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          {error && (
            <p role="alert" className="field-error">
              {error}
            </p>
          )}
          <button type="submit" disabled={submitting}>
            {submitting ? "Please wait…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
