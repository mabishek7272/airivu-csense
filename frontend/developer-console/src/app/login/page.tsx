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
    <main style={{ maxWidth: 380, margin: "10vh auto" }}>
      <h1 style={{ fontSize: 20 }}>AIRIVU CSense</h1>
      <p style={{ color: "#555", marginBottom: 24 }}>Developer Console — platform operator sign-in</p>
      <p style={{ color: "#a15c00", fontSize: 13, marginBottom: 16 }}>
        MFA enforcement for platform accounts lands in Phase&nbsp;2 (see CHECKLIST.md).
      </p>

      <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && (
          <p role="alert" style={{ color: "#b00020" }}>
            {error}
          </p>
        )}
        <button type="submit" disabled={submitting}>
          {submitting ? "Please wait…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
