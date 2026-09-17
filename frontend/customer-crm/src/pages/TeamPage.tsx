import { useState } from "react";
import type { Membership } from "../api/memberships";
import { inviteMember, listMemberships, updateMembership } from "../api/memberships";
import { ApiRequestError } from "../api/client";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Layout } from "../components/Layout";
import { useNotifications } from "../components/Notifications";
import {
  EmptyPanel,
  FailureState,
  InlineSpinner,
  LoadingRows,
} from "../components/States";
import { useOnlineStatus } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Labels shown for each role, shared between the invite dialog's role picker and the
 *  per-row role-change select on this page so the two stay in sync. */
type RoleName = "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";

const ROLE_LABELS: Record<RoleName, string> = {
  tenant_viewer: "Viewer (read-only)",
  tenant_member: "Member",
  tenant_operator: "Operator (cameras, rules, incidents)",
  tenant_owner: "Owner",
};

/** Short, sentence-friendly form (with article) for the role-change success toast -
 *  ROLE_LABELS' parenthetical descriptions read fine in a <select> but not mid-sentence. */
const ROLE_TOAST_PHRASE: Record<RoleName, string> = {
  tenant_viewer: "a viewer",
  tenant_member: "a member",
  tenant_operator: "an operator",
  tenant_owner: "an owner",
};

/** Team membership: who has access to this tenant, and what they can do.
 *
 *  `membership.manage` is owner-only, deliberately more restricted than any resource
 *  permission a member can hold - deciding who else can act on this account is the thing
 *  every other permission is downstream of. This page does not hide itself from a member
 *  without that permission (nothing in this app hides actions client-side - the backend
 *  enforces and a 403 renders through the same FailureState every other page already
 *  uses), it just fails informatively for them.
 */
export function TeamPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();
  const members = useResource(listMemberships, []);

  const [inviting, setInviting] = useState(false);
  const [revoking, setRevoking] = useState<Membership | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function handleStatusChange(member: Membership, status: "active" | "suspended") {
    setBusyId(member.id);
    try {
      const updated = await updateMembership(member.id, { status });
      members.mutate((current) => (current ?? []).map((m) => (m.id === updated.id ? updated : m)));
      notify.success(`${member.display_name} is now ${status}`);
    } catch (err) {
      notify.error(
        "Could not update this member",
        err instanceof ApiRequestError ? err.body.message : undefined,
      );
    } finally {
      setBusyId(null);
    }
  }

  async function handleRoleChange(member: Membership, role_name: RoleName) {
    setBusyId(member.id);
    try {
      const updated = await updateMembership(member.id, { role_name });
      members.mutate((current) => (current ?? []).map((m) => (m.id === updated.id ? updated : m)));
      notify.success(`${member.display_name} is now ${ROLE_TOAST_PHRASE[role_name]}`);
    } catch (err) {
      notify.error(
        "Could not change this member's role",
        err instanceof ApiRequestError ? err.body.message : undefined,
      );
    } finally {
      setBusyId(null);
    }
  }

  async function handleRevoke() {
    if (!revoking) return;
    setRevokeBusy(true);
    try {
      const updated = await updateMembership(revoking.id, { status: "revoked" });
      members.mutate((current) => (current ?? []).map((m) => (m.id === updated.id ? updated : m)));
      notify.success(`${revoking.display_name}'s access has been revoked`);
      setRevoking(null);
    } catch (err) {
      notify.error(
        "Could not revoke this member",
        err instanceof ApiRequestError ? err.body.message : undefined,
      );
    } finally {
      setRevokeBusy(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Team</h1>
          <p className="muted">
            {members.data ? `${members.data.length} member${members.data.length === 1 ? "" : "s"}` : " "}
          </p>
        </div>
        <button type="button" onClick={() => setInviting(true)} disabled={!online}>
          Invite member
        </button>
      </div>

      {members.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : Boolean(members.error) && !members.data ? (
        <FailureState error={members.error} online={online} onRetry={members.reload} entity="team members" />
      ) : members.data && members.data.length === 0 ? (
        <EmptyPanel title="No members yet" icon="◇">
          <p>Invite the first teammate to give them access to this tenant.</p>
        </EmptyPanel>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="data-table">
            <caption className="visually-hidden">Team members</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Role</th>
                <th scope="col">Status</th>
                <th scope="col">Scope</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(members.data ?? []).map((member) => (
                <tr key={member.id}>
                  <td>{member.display_name}</td>
                  <td className="mono">{member.email}</td>
                  <td>
                    <select
                      value={member.role_name}
                      disabled={busyId === member.id || !online}
                      onChange={(e) => void handleRoleChange(member, e.target.value as RoleName)}
                    >
                      <option value="tenant_viewer">{ROLE_LABELS.tenant_viewer}</option>
                      <option value="tenant_member">{ROLE_LABELS.tenant_member}</option>
                      <option value="tenant_operator">{ROLE_LABELS.tenant_operator}</option>
                      <option value="tenant_owner">{ROLE_LABELS.tenant_owner}</option>
                    </select>
                  </td>
                  <td>
                    <MembershipStatusBadge status={member.status} />
                  </td>
                  <td className="muted">{member.site_scope_mode === "all" ? "All sites" : "None"}</td>
                  <td className="row-actions">
                    {member.status === "active" && (
                      <button
                        type="button"
                        className="btn-quiet"
                        disabled={busyId === member.id || !online}
                        onClick={() => void handleStatusChange(member, "suspended")}
                      >
                        Suspend
                      </button>
                    )}
                    {member.status === "suspended" && (
                      <button
                        type="button"
                        className="btn-quiet"
                        disabled={busyId === member.id || !online}
                        onClick={() => void handleStatusChange(member, "active")}
                      >
                        Reactivate
                      </button>
                    )}
                    {member.status !== "revoked" && (
                      <button
                        type="button"
                        className="btn-quiet btn-danger-quiet"
                        disabled={busyId === member.id || !online}
                        onClick={() => setRevoking(member)}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {inviting && (
        <InviteDialog
          onClose={() => setInviting(false)}
          onInvited={(member) => {
            members.mutate((current) => [...(current ?? []), member]);
            notify.success(
              `${member.display_name} invited`,
              member.invitation_link
                ? "The invitation email could not be sent - share this link with them directly."
                : "An invitation email has been sent.",
            );
            setInviting(false);
          }}
        />
      )}

      <ConfirmDialog
        open={revoking !== null}
        title="Revoke this member's access?"
        busy={revokeBusy}
        confirmLabel="Revoke access"
        body={
          <p>
            <strong>{revoking?.display_name}</strong> ({revoking?.email}) will lose access
            to this tenant immediately. This can be undone by inviting them again.
          </p>
        }
        onConfirm={() => void handleRevoke()}
        onCancel={() => setRevoking(null)}
      />
    </Layout>
  );
}

function MembershipStatusBadge({ status }: { status: Membership["status"] }) {
  const cls =
    status === "active" ? "badge-low" : status === "invited" ? "badge-medium" :
    status === "suspended" ? "badge-medium" : "badge-critical";
  return <span className={`badge ${cls}`}>{status}</span>;
}

function InviteDialog({
  onClose,
  onInvited,
}: {
  onClose: () => void;
  onInvited: (member: import("../api/memberships").InviteResult) => void;
}) {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [roleName, setRoleName] = useState<RoleName>("tenant_member");
  const [siteScopeMode, setSiteScopeMode] = useState<"all" | "none">("none");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      const member = await inviteMember({
        email, display_name: displayName, role_name: roleName, site_scope_mode: siteScopeMode,
      });
      onInvited(member);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Could not send this invitation.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title="Invite a team member" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label>
          Name
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </label>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Role
          <select value={roleName} onChange={(e) => setRoleName(e.target.value as RoleName)}>
            <option value="tenant_viewer">{ROLE_LABELS.tenant_viewer}</option>
            <option value="tenant_member">{ROLE_LABELS.tenant_member}</option>
            <option value="tenant_operator">{ROLE_LABELS.tenant_operator}</option>
            <option value="tenant_owner">{ROLE_LABELS.tenant_owner}</option>
          </select>
        </label>
        <label>
          Site access
          <select value={siteScopeMode} onChange={(e) => setSiteScopeMode(e.target.value as "all" | "none")}>
            <option value="none">No sites (assign later)</option>
            <option value="all">All sites</option>
          </select>
          {/* Per-site scoping exists in the schema but has no picker UI yet - naming the
              gap here rather than pretending "None"/"All" is the whole story. */}
          <small className="muted">Picking specific sites isn't available yet.</small>
        </label>

        {submitting && <InlineSpinner label="Sending invitation…" />}
        {!submitting && error && (
          <p role="alert" className="error-panel">
            {error}
          </p>
        )}

        <div className="form-actions">
          <button
            type="button"
            className="primary"
            disabled={!email || !displayName || submitting}
            onClick={() => void handleSubmit()}
          >
            Send invitation
          </button>
        </div>
      </div>
    </Dialog>
  );
}
