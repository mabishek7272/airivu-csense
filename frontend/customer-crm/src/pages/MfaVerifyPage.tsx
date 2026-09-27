import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useBrand } from "../branding/BrandProvider";

/** The second step of login when the backend's `/api/v1/auth/login` returns
 *  `status: "mfa_required"` (see AuthContext's `mfaChallenge`) - reached only from
 *  LoginPage's own post-login effect, never linked to directly. Matches LoginPage's
 *  shell/field/button conventions and brand-aware heading for the same reason every
 *  other pre-auth page here does (a mid-login screen under a brand's own path showing
 *  the wrong name would be just as much a leak as LoginPage's own logo was). */
export function MfaVerifyPage() {
  const { mfaChallenge, verifyMfa, cancelMfa } = useAuth();
  const brand = useBrand();
  const navigate = useNavigate();

  const [code, setCode] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [useRecoveryCode, setUseRecoveryCode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // A direct navigation to this route, or a page refresh, loses the in-memory
  // mfaChallenge (it's never persisted - see AuthContext) - there's no challenge left
  // to verify against, so bounce back to /login rather than showing a broken form.
  useEffect(() => {
    if (!mfaChallenge) {
      navigate("/login", { replace: true });
    }
  }, [mfaChallenge, navigate]);

  if (!mfaChallenge) {
    return null;
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      if (useRecoveryCode) {
        await verifyMfa({ recovery_code: recoveryCode });
      } else {
        await verifyMfa({ code });
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

  function handleStartOver() {
    cancelMfa();
    navigate("/login");
  }

  const factorLabel =
    mfaChallenge.factor === "totp"
      ? "Enter the 6-digit code from your authenticator app."
      : "We emailed you a 6-digit code. Enter it below.";

  return (
    <main className="auth-shell">
      <div className="card auth-card">
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
        <p>{factorLabel}</p>

        <form onSubmit={handleSubmit}>
          {useRecoveryCode ? (
            <div className="field">
              <label htmlFor="recovery-code">Recovery code</label>
              <input
                id="recovery-code"
                value={recoveryCode}
                onChange={(e) => setRecoveryCode(e.target.value)}
                required
                autoComplete="one-time-code"
                autoFocus
              />
            </div>
          ) : (
            <div className="field">
              <label htmlFor="code">
                {mfaChallenge.factor === "totp" ? "Authenticator code" : "Emailed code"}
              </label>
              <input
                id="code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                pattern="\d{6}"
                maxLength={6}
                minLength={6}
                required
                autoComplete="one-time-code"
                autoFocus
              />
            </div>
          )}

          {error && (
            <p role="alert" style={{ color: "var(--critical)" }}>
              {error}
            </p>
          )}

          <button type="submit" className="primary" disabled={submitting} style={{ width: "100%" }}>
            {submitting ? "Please wait…" : "Verify"}
          </button>
        </form>

        {mfaChallenge.factor === "totp" && (
          <button
            type="button"
            className="link"
            onClick={() => {
              setUseRecoveryCode(!useRecoveryCode);
              setError(null);
            }}
            style={{ marginTop: 16 }}
          >
            {useRecoveryCode ? "Use your authenticator code instead" : "Use a recovery code instead"}
          </button>
        )}

        <button type="button" className="link" onClick={handleStartOver} style={{ marginTop: 8 }}>
          Start over
        </button>
      </div>
    </main>
  );
}
