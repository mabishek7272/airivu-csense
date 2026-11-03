"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ApiRequestError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";

/** Subtle pointer-tilt + shine on the auth card - same technique as the customer CRM's
 *  own login page (frontend/customer-crm/src/pages/LoginPage.tsx's useAuthCardTilt),
 *  which itself reuses the apex splash's tilt approach: mutate style via refs rather
 *  than React state so a mousemove at 60fps never triggers a re-render. */
function useAuthCardTilt() {
  const stageRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const shineRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const stage = stageRef.current;
    const card = cardRef.current;
    const shine = shineRef.current;
    if (!stage || !card || !shine) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const onMove = (e: PointerEvent) => {
      const rect = stage.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      const y = ((e.clientY - rect.top) / rect.height) * 2 - 1;
      const maxTilt = 6;
      card.style.transform = `rotateX(${(-y * maxTilt).toFixed(2)}deg) rotateY(${(x * maxTilt).toFixed(2)}deg)`;
      shine.style.background = `radial-gradient(circle at ${50 + x * 40}% ${50 + y * 40}%, rgb(255 255 255 / 8%), transparent 55%)`;
    };
    const onLeave = () => {
      card.style.transform = "rotateX(0deg) rotateY(0deg)";
      shine.style.background = "none";
    };
    stage.addEventListener("pointermove", onMove);
    stage.addEventListener("pointerleave", onLeave);
    return () => {
      stage.removeEventListener("pointermove", onMove);
      stage.removeEventListener("pointerleave", onLeave);
    };
  }, []);

  return { stageRef, cardRef, shineRef };
}

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const { stageRef, cardRef, shineRef } = useAuthCardTilt();
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
    <div className="auth-shell" ref={stageRef}>
      <div className="auth-blob auth-blob-accent" />
      <div className="auth-blob auth-blob-violet" />
      <div className="auth-card-stage" ref={cardRef}>
        <div className="card auth-card">
          <div className="auth-card-shine" ref={shineRef} />
          <h1>AIRIVU CSense</h1>
          <p>Developer Console — platform operator sign-in</p>

          <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="field">
              <label htmlFor="login-email">Email</label>
              <input id="login-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
            </div>
            <div className="field">
              <label htmlFor="login-password">Password</label>
              <input id="login-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
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
    </div>
  );
}
