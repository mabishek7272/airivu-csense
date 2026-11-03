import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useBrand } from "../branding/BrandProvider";

/** Subtle pointer-tilt + shine on the auth card, same technique (and same reasoning for
 *  mutating style via refs instead of React state) as the apex splash's own 3D tilt -
 *  see landing3rdi/useTiltGlare.ts and Landing3rdiPage's docstring. Scaled down (6deg vs
 *  the splash's bigger tilt) since this is a form people are trying to use quickly, not
 *  a one-off marketing moment. */
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

export function LoginPage() {
  const { login, register } = useAuth();
  const brand = useBrand();
  const navigate = useNavigate();
  const { stageRef, cardRef, shineRef } = useAuthCardTilt();
  // A URL that resolves to (or was clearly meant to look like) a specific brand's own
  // private login page must never offer "create a brand-new, unrelated organization"
  // — that's a real security-relevant surface, not just a visual leak, so this checks
  // `hasSlug` (was a brand path present at all) rather than `isDefaultBrand` (did it
  // actually resolve) - a misspelled/unresolved brand slug should still hide this.
  const allowSelfRegistration = !brand.hasSlug;
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
    <main className="auth-shell" ref={stageRef}>
      <div className="auth-blob auth-blob-accent" />
      <div className="auth-blob auth-blob-violet" />
      <div className="auth-card-stage" ref={cardRef}>
      <div className="card auth-card">
        <div className="auth-card-shine" ref={shineRef} />
        {/* The default (unbranded) login is 3RDI's own identity - the parent company
            operating every white-label brand on this platform (Airivu CSense, eAIgleye,
            Apti SafeGuard), not any one of them. It must never reuse a specific brand's
            mark (this page briefly used Airivu CSense's logo here, which wrongly implied
            Airivu was "the default" rather than a peer white-label brand like the other
            two - fixed). /3rdi-logo.png is the same asset the apex splash
            (Landing3rdiPage) uses, so the parent identity looks identical wherever it
            appears. There is no org_branding row for "no brand at all", so nothing to
            fetch here. */}
        {!brand.isDefaultBrand && brand.logoUrl ? (
          <img src={brand.logoUrl} alt="" style={{ height: 40, marginBottom: 12, objectFit: "contain" }} />
        ) : brand.isDefaultBrand ? (
          <img
            src="/3rdi-logo.png"
            alt=""
            style={{ height: 40, marginBottom: 12, objectFit: "contain" }}
          />
        ) : null}
        <h1>{brand.isDefaultBrand ? "3RDI" : brand.displayName}</h1>
        <p>{mode === "login" ? "Sign in to your workspace" : "Create your organization"}</p>

        <form onSubmit={handleSubmit}>
          {mode === "register" && allowSelfRegistration && (
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

        {allowSelfRegistration && (
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
        )}
      </div>
      </div>
    </main>
  );
}
