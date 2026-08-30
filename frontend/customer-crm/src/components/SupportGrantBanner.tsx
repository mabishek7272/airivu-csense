import { useEffect, useState } from "react";
import type { SupportGrant } from "../api/supportGrants";
import { listActiveSupportGrants, revokeSupportGrant } from "../api/supportGrants";
import { ConfirmDialog } from "./Dialog";
import { useNotifications } from "./Notifications";

const POLL_INTERVAL_MS = 60_000;

/** "AIRIVU support is currently accessing your account" - the tenant-facing half of the
 *  just-in-time support grant lifecycle (backend/tenant_api/app/api/support.py). A
 *  standing condition, not an event, so it lives as a persistent top banner like the
 *  offline banner does (Notifications.tsx) - not a toast, and not dismissible without
 *  actually ending the session, since dismissing it would leave someone thinking support
 *  access ended when it did not.
 *
 *  Polled rather than pushed: there is no realtime channel this app has for
 *  platform-side events, and a support session opening is not urgent enough to justify
 *  building one just for this.
 */
export function SupportGrantBanner() {
  const notify = useNotifications();
  const [grants, setGrants] = useState<SupportGrant[]>([]);
  const [revoking, setRevoking] = useState<SupportGrant | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      setGrants(await listActiveSupportGrants());
    } catch {
      // A failed poll must not itself become a visible error - it will simply try again
      // in a minute, and a person is never worse off than before this component existed.
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);

  async function handleRevoke() {
    if (!revoking) return;
    setBusy(true);
    try {
      await revokeSupportGrant(revoking.id, "Ended by the tenant from the active-session banner.");
      setGrants((current) => current.filter((g) => g.id !== revoking.id));
      notify.success("Support session ended");
      setRevoking(null);
    } catch {
      notify.error("Could not end this support session", "Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  if (grants.length === 0) return null;

  return (
    <>
      {grants.map((grant) => (
        <div key={grant.id} className="top-banner top-banner-offline" role="status">
          <span aria-hidden="true">🛈</span>
          <span>
            <strong>AIRIVU support is currently accessing your account</strong> ({grant.developer_email},
            ticket {grant.ticket_reference}) - ends automatically by{" "}
            {new Date(grant.expires_at).toLocaleString()}.
          </span>
          <button type="button" className="btn-quiet" onClick={() => setRevoking(grant)}>
            End session now
          </button>
        </div>
      ))}

      <ConfirmDialog
        open={revoking !== null}
        title="End this support session?"
        busy={busy}
        confirmLabel="End session"
        body={
          <p>
            This immediately revokes <strong>{revoking?.developer_email}</strong>'s access granted
            under ticket <strong>{revoking?.ticket_reference}</strong>. They will need a new approved
            grant to regain access.
          </p>
        }
        onConfirm={() => void handleRevoke()}
        onCancel={() => setRevoking(null)}
      />
    </>
  );
}
