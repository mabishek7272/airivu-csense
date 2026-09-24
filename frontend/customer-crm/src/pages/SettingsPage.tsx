import { useEffect, useState } from "react";
import { ApiRequestError } from "../api/client";
import { getOwnLicense, listDevices, removeDevice } from "../api/license";
import { confirmTotp, enrollTotp, getMfaStatus, removeTotp, verifyMfa } from "../api/mfa";
import { Dialog } from "../components/Dialog";
import { Layout, relativeTime } from "../components/Layout";
import { useNotifications } from "../components/Notifications";
import { EmptyPanel, FailureState, InlineSpinner, LoadingRows } from "../components/States";
import { useOnlineStatus } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Tenant settings: this tenant's own license, and each signed-in person's own
 *  two-factor authentication. Neither is a permission-gated resource the way
 *  memberships/cameras are - a license is informational (`license.read`, granted to
 *  every member), and MFA belongs to the account holding it, not the tenant.
 */
export function SettingsPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();
  const license = useResource(getOwnLicense, []);
  const mfa = useResource(getMfaStatus, []);
  const devices = useResource(listDevices, []);
  const [removingDeviceId, setRemovingDeviceId] = useState<string | null>(null);

  const [enrolling, setEnrolling] = useState(false);
  const [removing, setRemoving] = useState(false);

  async function handleRemoveDevice(id: string, name: string | null) {
    setRemovingDeviceId(id);
    try {
      await removeDevice(id);
      devices.reload();
      notify.success(`${name ?? "The device"} was removed`, "It is signed out and its slot is free.");
    } catch (err) {
      notify.error("Could not remove the device", err instanceof Error ? err.message : undefined);
    } finally {
      setRemovingDeviceId(null);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="muted">This tenant's license, and your own account security.</p>
        </div>
      </div>

      <section style={{ marginBottom: "var(--space-5)" }}>
        <h2>License</h2>
        {license.loading ? (
          <LoadingRows rows={2} columns={2} />
        ) : license.error ? (
          <FailureState error={license.error} online={online} onRetry={license.reload} entity="the license" />
        ) : license.data === null ? (
          <EmptyPanel title="No license assigned yet" icon="◇">
            <p>Contact your reseller or AIRIVU to have a plan issued to this tenant.</p>
          </EmptyPanel>
        ) : (
          <div className="card" style={{ padding: "var(--space-4)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
              <strong>{license.data.plan_name}</strong>
              <span
                className={`badge ${
                  license.data.status === "active"
                    ? "badge-low"
                    : license.data.status === "grace" || license.data.status === "scheduled"
                      ? "badge-medium"
                      : "badge-critical"
                }`}
              >
                {license.data.status}
              </span>
            </div>
            <p className="muted" style={{ marginTop: "var(--space-1)" }}>
              Started {new Date(license.data.starts_at).toLocaleDateString()}
              {license.data.expires_at
                ? ` · Expires ${new Date(license.data.expires_at).toLocaleDateString()}`
                : " · No expiry"}
              {license.data.status === "grace" && license.data.grace_ends_at
                ? ` · Renew by ${new Date(license.data.grace_ends_at).toLocaleDateString()} to avoid interruption`
                : null}
              {(license.data.status === "expired" || license.data.status === "suspended") &&
                " · Contact your reseller or AIRIVU to restore access"}
            </p>

            {license.data.quota_usage.length > 0 && (
              <div style={{ marginTop: "var(--space-3)" }}>
                {license.data.quota_usage.map((q) => (
                  <div key={q.quota_code} style={{ marginBottom: "var(--space-2)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                      <span className="mono">{q.quota_code}</span>
                      <span className="muted">
                        {q.consumed_value + q.reserved_value} / {q.limit_value}
                      </span>
                    </div>
                    <div className="quota-bar">
                      <div
                        className="quota-bar-fill"
                        style={{
                          width: `${Math.min(100, ((q.consumed_value + q.reserved_value) / q.limit_value) * 100)}%`,
                        }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      <section style={{ marginBottom: "var(--space-5)" }}>
        <h2>Devices</h2>
        {devices.loading ? (
          <LoadingRows rows={2} columns={3} />
        ) : devices.error || !devices.data ? (
          <FailureState error={devices.error} online={online} onRetry={devices.reload} entity="the device list" />
        ) : (
          <div className="card">
            <p className="muted">
              {devices.data.devices.length}
              {devices.data.device_limit !== null ? ` of ${devices.data.device_limit}` : ""} phones and
              tablets signed in
              {devices.data.web_session_limit !== null
                ? `. Your plan allows ${devices.data.web_session_limit} browser${
                    devices.data.web_session_limit === 1 ? "" : "s"
                  } signed in at once; signing in on another browser signs out the oldest.`
                : "."}
            </p>
            {devices.data.devices.length > 0 && (
              <table className="data-table">
                <caption className="visually-hidden">Signed-in devices</caption>
                <thead>
                  <tr>
                    <th scope="col">Device</th>
                    <th scope="col">Last active</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {devices.data.devices.map((device) => (
                    <tr key={device.id}>
                      <td>
                        <strong>{device.name ?? "Unnamed device"}</strong>
                        {device.platform && <div className="muted">{device.platform}</div>}
                      </td>
                      <td title={device.last_seen_at}>{relativeTime(device.last_seen_at)}</td>
                      <td className="row-actions">
                        <button
                          type="button"
                          className="btn-quiet btn-danger-quiet"
                          disabled={!online || removingDeviceId === device.id}
                          onClick={() => void handleRemoveDevice(device.id, device.name)}
                        >
                          {removingDeviceId === device.id ? "Removing…" : "Remove"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>

      <section>
        <h2>Two-factor authentication</h2>
        {mfa.loading ? (
          <LoadingRows rows={1} columns={2} />
        ) : mfa.error ? (
          <FailureState error={mfa.error} online={online} onRetry={mfa.reload} entity="MFA status" />
        ) : mfa.data ? (
          <div className="card" style={{ padding: "var(--space-4)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{mfa.data.enrolled ? "Turned on" : "Turned off"}</strong>
              {mfa.data.enrolled && (
                <p className="muted" style={{ margin: 0 }}>
                  {mfa.data.recovery_codes_remaining} recovery code{mfa.data.recovery_codes_remaining === 1 ? "" : "s"} remaining
                </p>
              )}
            </div>
            {mfa.data.enrolled ? (
              <button type="button" className="btn-quiet btn-danger-quiet" disabled={!online} onClick={() => setRemoving(true)}>
                Turn off
              </button>
            ) : (
              <button type="button" className="primary" disabled={!online} onClick={() => setEnrolling(true)}>
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
          <p className="mono" style={{ wordBreak: "break-all", background: "var(--surface-sunken)", padding: "var(--space-2)", borderRadius: "var(--radius-sm)" }}>
            {secret}
          </p>
          <small className="muted">Or use this link on a device with the app installed:</small>
          <p className="mono" style={{ wordBreak: "break-all", fontSize: 12 }}>{otpauthUri}</p>

          <label>
            Enter the 6-digit code from your app
            <input
              inputMode="numeric"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
            />
          </label>

          {submitting && <InlineSpinner label="Confirming…" />}
          {!submitting && error && (
            <p role="alert" className="error-panel">
              {error}
            </p>
          )}

          <div className="form-actions">
            <button type="button" className="primary" disabled={code.length !== 6 || submitting} onClick={() => void handleConfirm()}>
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
            <button type="button" className="primary" onClick={onEnrolled}>
              I've saved these codes
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
  const [stepUpRequired, setStepUpRequired] = useState(false);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      // First: verify MFA code for step-up
      await verifyMfa({ code });
      // Second: remove MFA (this can now proceed with step-up verified)
      await removeTotp();
      onRemoved();
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 403 && err.body.code === "step_up_required") {
        setStepUpRequired(true);
        setError("Step-up verification required. Enter your MFA code again.");
      } else {
        setError(err instanceof ApiRequestError ? err.body.message : "Could not turn off two-factor authentication.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title="Turn off two-factor authentication" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p>Confirm with a current code from your authenticator app to turn this off.</p>
        {stepUpRequired && (
          <p className="muted" style={{ fontSize: "0.9em" }}>
            This is a sensitive operation and requires recent authentication verification.
          </p>
        )}
        <label>
          6-digit code
          <input
            inputMode="numeric"
            maxLength={6}
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
            placeholder="000000"
            autoFocus
          />
        </label>

        {submitting && <InlineSpinner label="Turning off…" />}
        {!submitting && error && (
          <p role="alert" className="error-panel">
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
