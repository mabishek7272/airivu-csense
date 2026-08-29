import { useMemo, useState } from "react";
import type { Channel, EscalationStep, NotificationPolicy } from "../api/notifications";
import {
  CHANNELS,
  SEVERITIES,
  createPolicy,
  deletePolicy,
  listPolicies,
  listRecipientGroups,
  publishVersion,
  updatePolicy,
} from "../api/notifications";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import {
  ErrorSummary,
  Field,
  FormActions,
  combine,
  maxLength,
  onSubmitHandler,
  required,
  useForm,
} from "../components/Form";
import type { Severity } from "../api/types";
import { SeverityBadge } from "../components/Badges";
import { Layout } from "../components/Layout";
import { useNotifications } from "../components/Notifications";
import {
  EmptyPanel,
  FailureState,
  InlineSpinner,
  LoadingRows,
  NoResultsPanel,
  SlowNetworkNotice,
} from "../components/States";
import { useOnlineStatus, useSlowRequest } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Notification policies — which escalation ladder applies to which incident.
 *
 *  A policy with no published version is a deliberate draft state: it exists, but reaches
 *  nobody until a version is published, because the dispatcher only ever looks at a
 *  policy's *active* version. Publishing never edits a past version - it always adds a new
 *  one, so "why was I called at 3am" stays answerable after the ladder has since changed.
 *
 *  Quiet hours are not set here - see Recipient groups. Two people in the same step can
 *  legitimately want to be reached at different hours, so the schedule lives on each
 *  person, not on the step.
 */

const CHANNEL_LABELS: Record<Channel, string> = {
  email: "Email",
  whatsapp: "WhatsApp",
  in_app: "In-app",
  sms: "SMS",
  web_push: "Web push",
  webhook: "Webhook",
};

export function NotificationPoliciesPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<NotificationPolicy | null>(null);
  const [building, setBuilding] = useState<NotificationPolicy | null>(null);
  const [deleting, setDeleting] = useState<NotificationPolicy | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);

  const policies = useResource(listPolicies, []);
  const groups = useResource(listRecipientGroups, []);
  const slow = useSlowRequest(policies.loading);

  const visible = useMemo(() => {
    if (!policies.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return policies.data;
    return policies.data.filter((p) => p.name.toLowerCase().includes(term));
  }, [policies.data, search]);

  async function toggleStatus(policy: NotificationPolicy) {
    setToggling(policy.id);
    const next = policy.status === "active" ? "disabled" : "active";
    try {
      const updated = await updatePolicy(policy.id, { status: next });
      policies.mutate((current) =>
        (current ?? []).map((p) => (p.id === policy.id ? updated : p)),
      );
      notify.success(`${policy.name} was ${next}`);
    } catch (err) {
      notify.error(
        `Could not update ${policy.name}`,
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setToggling(null);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deletePolicy(removed.id);
      policies.mutate((current) => (current ?? []).filter((p) => p.id !== removed.id));
      notify.success(`${removed.name} was removed`);
      setDeleting(null);
    } catch (err) {
      notify.error(
        `Could not remove ${removed.name}`,
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Notification policies</h1>
          <p className="muted">
            {policies.data
              ? `${policies.data.length} polic${policies.data.length === 1 ? "y" : "ies"}`
              : " "}
            {policies.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setCreating(true)} disabled={!online}>
          Add policy
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="policy-search">
          Search policies
        </label>
        <input
          id="policy-search"
          type="search"
          placeholder="Search by name"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {slow && policies.loading && <SlowNetworkNotice />}

      {policies.loading ? (
        <LoadingRows rows={3} columns={4} />
      ) : Boolean(policies.error) && !policies.data ? (
        <FailureState
          error={policies.error}
          online={online}
          onRetry={policies.reload}
          entity="notification policy"
        />
      ) : visible.length === 0 && search.trim() ? (
        <NoResultsPanel query={search.trim()} entity="policies" onClear={() => setSearch("")} />
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No notification policies yet"
          icon="🔔"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Create your first policy
            </button>
          }
        >
          <p>
            Without a published policy, an incident opens but nobody is told — the
            platform falls back to notifying every recipient group immediately, which is
            a floor, not a plan. A policy decides who is told, in what order, and how
            urgently.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Notification policies</caption>
            <thead>
              <tr>
                <th scope="col">Policy</th>
                <th scope="col">Applies to</th>
                <th scope="col">Escalation</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((p) => (
                <tr key={p.id}>
                  <td>
                    <strong>{p.name}</strong>
                  </td>
                  <td>
                    {p.severities.length === 0 ? (
                      <span className="muted">Every severity</span>
                    ) : (
                      <div className="chip-row">
                        {p.severities.map((s) => (
                          <SeverityBadge key={s} severity={s as Severity} />
                        ))}
                      </div>
                    )}
                  </td>
                  <td>
                    {p.active_version ? (
                      <>
                        {p.active_version.steps.length} step
                        {p.active_version.steps.length === 1 ? "" : "s"}
                        <div className="muted">v{p.active_version.version_number}</div>
                      </>
                    ) : (
                      <span className="muted">Draft — not published</span>
                    )}
                  </td>
                  <td>
                    <span className={`pill pill-${p.status}`}>{p.status}</span>
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setBuilding(p)}
                      disabled={!online}
                    >
                      Escalation
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => void toggleStatus(p)}
                      disabled={!online || toggling === p.id}
                    >
                      {p.status === "active" ? "Disable" : "Enable"}
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(p)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(p)}
                      disabled={!online}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(creating || editing) && (
        <PolicyFormDialog
          policy={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(policy, wasNew) => {
            policies.mutate((current) => {
              const list = current ?? [];
              return wasNew
                ? [...list, policy]
                : list.map((p) => (p.id === policy.id ? policy : p));
            });
            notify.success(
              wasNew ? `${policy.name} was created` : `${policy.name} was updated`,
              wasNew ? "Build its escalation next, or it reaches nobody." : undefined,
            );
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      {building && (
        <EscalationDialog
          policy={building}
          groups={groups.data ?? []}
          onClose={() => setBuilding(null)}
          onPublished={(updated) => {
            policies.mutate((current) =>
              (current ?? []).map((p) => (p.id === updated.id ? updated : p)),
            );
            notify.success(
              `${updated.name} published as v${updated.active_version?.version_number}`,
            );
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this policy?"
        busy={deleteBusy}
        confirmLabel="Remove policy"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> will be removed entirely.
            </p>
            <p className="muted">
              If it has ever produced a notification, removal is refused instead — that
              history stays explainable. Disable it in that case; a disabled policy is
              skipped by every future incident.
            </p>
          </>
        }
        onConfirm={() => void handleDelete()}
        onCancel={() => setDeleting(null)}
      />
    </Layout>
  );
}

/* ------------------------------------------------------------------ create / edit */

function PolicyFormDialog({
  policy,
  onClose,
  onSaved,
}: {
  policy: NotificationPolicy | null;
  onClose: () => void;
  onSaved: (policy: NotificationPolicy, wasNew: boolean) => void;
}) {
  const isNew = policy === null;
  const [severities, setSeverities] = useState<string[]>(policy?.severities ?? []);

  const form = useForm({
    name: {
      initial: policy?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(120, "Name")),
    },
  });

  function toggleSeverity(s: string) {
    setSeverities((current) =>
      current.includes(s) ? current.filter((x) => x !== s) : [...current, s],
    );
  }

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const body = { name: form.values.name, severities };
      const saved = isNew
        ? await createPolicy(body)
        : await updatePolicy(policy.id, body);
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={isNew ? "Create notification policy" : `Edit ${policy.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("name")} label="Name" required placeholder="Dock watch" />

        <div className="field">
          <label>Applies to</label>
          <p className="field-hint">
            Leave every severity unchecked to match all of them.
          </p>
          <div className="chip-row">
            {SEVERITIES.map((s) => (
              <label key={s} className="chip">
                <input
                  type="checkbox"
                  checked={severities.includes(s)}
                  onChange={() => toggleSeverity(s)}
                />
                {s}
              </label>
            ))}
          </div>
        </div>

        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Create policy" : "Save changes"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}

/* --------------------------------------------------------------------- escalation */

function EscalationDialog({
  policy,
  groups,
  onClose,
  onPublished,
}: {
  policy: NotificationPolicy;
  groups: { id: string; name: string }[];
  onClose: () => void;
  onPublished: (policy: NotificationPolicy) => void;
}) {
  const [steps, setSteps] = useState<EscalationStep[]>(
    policy.active_version?.steps.map((s) => ({ ...s })) ?? [
      { level: 0, delay_seconds: 0, channels: ["email"], recipient_group_ids: [] },
    ],
  );
  const [publishing, setPublishing] = useState(false);
  const [error, setError] = useState<string | undefined>();

  function addStep() {
    setSteps((current) => [
      ...current,
      { level: current.length, delay_seconds: 300, channels: ["email"], recipient_group_ids: [] },
    ]);
  }

  function removeStep(index: number) {
    setSteps((current) =>
      current.filter((_, i) => i !== index).map((s, i) => ({ ...s, level: i })),
    );
  }

  function updateStep(index: number, patch: Partial<EscalationStep>) {
    setSteps((current) => current.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function toggleChannel(index: number, c: Channel) {
    const step = steps[index];
    const channels = step.channels.includes(c)
      ? step.channels.filter((x) => x !== c)
      : [...step.channels, c];
    updateStep(index, { channels });
  }

  function toggleGroup(index: number, groupId: string) {
    const step = steps[index];
    const ids = step.recipient_group_ids.includes(groupId)
      ? step.recipient_group_ids.filter((x) => x !== groupId)
      : [...step.recipient_group_ids, groupId];
    updateStep(index, { recipient_group_ids: ids });
  }

  async function handlePublish() {
    setError(undefined);
    const incomplete = steps.find(
      (s) => s.channels.length === 0 || s.recipient_group_ids.length === 0,
    );
    if (incomplete) {
      setError(
        "Every step needs at least one channel and one recipient group - a step with " +
          "neither can never reach anyone.",
      );
      return;
    }
    setPublishing(true);
    try {
      const updated = await publishVersion(policy.id, steps);
      onPublished(updated);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not publish.");
    } finally {
      setPublishing(false);
    }
  }

  return (
    <Dialog
      open
      title={`Escalation for ${policy.name}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn-quiet" onClick={onClose}>
            Cancel
          </button>
          <button type="button" onClick={() => void handlePublish()} disabled={publishing}>
            {publishing ? "Publishing…" : "Publish"}
          </button>
        </>
      }
    >
      {error && (
        <div className="notice notice-warning" role="alert" style={{ marginBottom: 16 }}>
          <span aria-hidden="true">⚠</span>
          <p>{error}</p>
        </div>
      )}

      {groups.length === 0 && (
        <div className="notice notice-info" role="status" style={{ marginBottom: 16 }}>
          <span aria-hidden="true">ⓘ</span>
          <p>No recipient groups exist yet — create one first, or every step here is empty.</p>
        </div>
      )}

      <p className="muted">
        Publishing adds a new version rather than editing the last one — past
        notifications keep pointing at what actually produced them.
      </p>

      {steps.map((step, index) => (
        <div key={index} className="card" style={{ padding: 16, marginBottom: 12 }}>
          <div className="field-row">
            <strong>Step {index + 1}</strong>
            {steps.length > 1 && (
              <button
                type="button"
                className="btn-quiet btn-danger-quiet"
                onClick={() => removeStep(index)}
              >
                Remove step
              </button>
            )}
          </div>

          <div className="field">
            <label htmlFor={`delay-${index}`}>Wait before this step (seconds)</label>
            <input
              id={`delay-${index}`}
              type="number"
              min={0}
              max={86400}
              value={step.delay_seconds}
              onChange={(e) => updateStep(index, { delay_seconds: Number(e.target.value) })}
            />
          </div>

          <div className="field">
            <label>Channels</label>
            <div className="chip-row">
              {CHANNELS.map((c) => (
                <label key={c} className="chip">
                  <input
                    type="checkbox"
                    checked={step.channels.includes(c)}
                    onChange={() => toggleChannel(index, c)}
                  />
                  {CHANNEL_LABELS[c]}
                </label>
              ))}
            </div>
          </div>

          <div className="field">
            <label>Recipient groups</label>
            <div className="chip-row">
              {groups.map((g) => (
                <label key={g.id} className="chip">
                  <input
                    type="checkbox"
                    checked={step.recipient_group_ids.includes(g.id)}
                    onChange={() => toggleGroup(index, g.id)}
                  />
                  {g.name}
                </label>
              ))}
            </div>
          </div>
        </div>
      ))}

      <button type="button" className="btn-quiet" onClick={addStep}>
        Add another step
      </button>
    </Dialog>
  );
}
