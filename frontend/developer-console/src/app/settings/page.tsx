"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/auth/AuthContext";
import { ApiRequestError } from "@/api/client";
import { getMfaStatus, enrollTotp, confirmTotp, removeTotp, verifyMfa } from "@/api/mfa";
import { Dialog } from "@/components/Dialog";
import { Layout } from "@/components/Layout";
import { useNotifications } from "@/components/Notifications";
import { FailureState, InlineSpinner, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

/** This platform developer's own two-factor authentication. A recent verification here
 *  is what `POST /api/v1/admin/licenses` requires before it will issue - see
 *  components/IssueLicenseDialog.tsx.
 */
export default function SettingsPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const online = useOnlineStatus();
  const notify = useNotifications();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  const mfa = useResource(getMfaStatus, [isAuthenticated]);
  const [enrolling, setEnrolling] = useState(false);
  const [removing, setRemoving] = useState(false);

  if (isLoading || !isAuthenticated) {
    return (
      <div className="auth-shell" aria-busy="true">
        <p>Loading…</p>
      </div>
    );
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p>Your own account security.</p>
        </div>
      </div>

      <section>
        <h2>Two-factor authentication</h2>
        <p>
          Required for a recent, checkable step-up on high-risk actions (TRD-SEC-010) - today that means
          issuing a license.
        </p>

        {mfa.loading ? (
          <LoadingRows rows={1} columns={2} />
        ) : mfa.error ? (
          <FailureState error={mfa.error} online={online} onRetry={mfa.reload} entity="MFA status" />
        ) : mfa.data ? (
          <div className="card" style={{ padding: 16, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{mfa.data.enrolled ? "Turned on" : "Turned off"}</strong>
              {mfa.data.enrolled && (
                <p style={{ margin: 0 }}>
                  {mfa.data.recovery_codes_remaining} recovery code{mfa.data.recovery_codes_remaining === 1 ? "" : "s"} remaining
                </p>
              )}
            </div>
            {mfa.data.enrolled ? (
              <button type="button" className="btn-quiet btn-danger-quiet" disabled={!online} onClick={() => setRemoving(true)}>
                Turn off
              </button>
            ) : (
              <button type="button" disabled={!online} onClick={() => setEnrolling(true)}>
                Turn on
              </button>
            )}
          </div>
        ) : null}
      </section>

      {enrolling && (
        <EnrollMfaDialog
          onClose={() => setEnrolling(false)}
          onEnrolled={() => {
            mfa.reload();
            setEnrolling(false);
            notify.success("Two-factor authentication is on");
          }}
        />
      )}

      {removing && (
        <RemoveMfaDialog
          onClose={() => setRemoving(false)}
          onRemoved={() => {
            mfa.reload();
            setRemoving(false);
            notify.success("Two-factor authentication turned off");
          }}
        />
      )}
    </Layout>
  );
}

function EnrollMfaDialog({ onClose, onEnrolled }: { onClose: () => void; onEnrolled: () => void }) {
  const [phase, setPhase] = useState<"loading" | "scan" | "codes">("loading");
  const [secret, setSecret] = useState("");
  const [otpauthUri, setOtpauthUri] = useState("");
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void enrollTotp().then((res) => {
      if (cancelled) return;
      setSecret(res.secret);
      setOtpauthUri(res.otpauth_uri);
      setPhase("scan");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleConfirm() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await confirmTotp(code);
      setRecoveryCodes(res.recovery_codes);
      setPhase("codes");
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Could not confirm this code.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title="Turn on two-factor authentication" onClose={onClose}>
      {phase === "loading" && <InlineSpinner label="Setting up…" />}

      {phase === "scan" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p>Add this to an authenticator app (Google Authenticator, 1Password, Authy):</p>
          <p className="mono" style={{ wordBreak: "break-all", background: "var(--surface-sunken)", padding: 8, borderRadius: 5 }}>
            {secret}
          </p>
          <small>Or use this link on a device with the app installed:</small>
          <p className="mono" style={{ wordBreak: "break-all", fontSize: 12 }}>{otpauthUri}</p>

          <div className="field">
            <label htmlFor="mfa-code">Enter the 6-digit code from your app</label>
            <input
              id="mfa-code"
              inputMode="numeric"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
            />
          </div>

          {submitting && <InlineSpinner label="Confirming…" />}
          {!submitting && error && (
            <p role="alert" className="field-error">
              {error}
            </p>
          )}

          <div className="form-actions">
            <button type="button" disabled={code.length !== 6 || submitting} onClick={() => void handleConfirm()}>
              Confirm
            </button>
          </div>
        </div>
      )}

      {phase === "codes" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p>
            <strong>Save these recovery codes.</strong> Each can be used once if you lose access to your
            authenticator app. They will not be shown again.
          </p>
          <ul className="mono" style={{ columns: 2, listStyle: "none", padding: 0, margin: 0 }}>
            {recoveryCodes.map((rc) => (
              <li key={rc}>{rc}</li>
            ))}
          </ul>
          <div className="form-actions">
            <button type="button" onClick={onEnrolled}>
              I&apos;ve saved these codes
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function RemoveMfaDialog({ onClose, onRemoved }: { onClose: () => void; onRemoved: () => void }) {
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      await verifyMfa({ code });
      await removeTotp();
      onRemoved();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Could not turn off two-factor authentication.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title="Turn off two-factor authentication" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p>Confirm with a current code from your authenticator app to turn this off.</p>
        <div className="field">
          <label htmlFor="remove-mfa-code">6-digit code</label>
          <input
            id="remove-mfa-code"
            inputMode="numeric"
            maxLength={6}
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          />
        </div>

        {submitting && <InlineSpinner label="Turning off…" />}
        {!submitting && error && (
          <p role="alert" className="field-error">
            {error}
          </p>
        )}

        <div className="form-actions">
          <button
            type="button"
            className="btn-danger"
            disabled={code.length !== 6 || submitting}
            onClick={() => void handleSubmit()}
          >
            Turn off
          </button>
        </div>
      </div>
    </Dialog>
  );
}
