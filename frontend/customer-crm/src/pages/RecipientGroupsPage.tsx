import { useMemo, useState } from "react";
import type { Channel, RecipientGroup, RecipientMember } from "../api/notifications";
import {
  CHANNELS,
  addMember,
  createRecipientGroup,
  deleteRecipientGroup,
  listMembers,
  listRecipientGroups,
  removeMember,
  updateMember,
  updateRecipientGroup,
} from "../api/notifications";
import { listTimezones } from "../api/sites";
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

/** Recipient groups — who a notification policy tells, and how.
 *
 *  A member's quiet hours are their own, not the group's or the policy's - two people in
 *  the same group can be reached at different times of night, which is why the schedule
 *  lives on each person rather than somewhere shared. `high` and `critical` incidents
 *  reach everyone regardless of what they set - a fire alarm ignores quiet hours.
 */

const CHANNEL_LABELS: Record<Channel, string> = {
  email: "Email",
  whatsapp: "WhatsApp",
  in_app: "In-app",
  sms: "SMS",
  web_push: "Web push",
  webhook: "Webhook",
};

export function RecipientGroupsPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<RecipientGroup | null>(null);
  const [managing, setManaging] = useState<RecipientGroup | null>(null);
  const [deleting, setDeleting] = useState<RecipientGroup | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const groups = useResource(listRecipientGroups, []);
  const slow = useSlowRequest(groups.loading);

  const visible = useMemo(() => {
    if (!groups.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return groups.data;
    return groups.data.filter((g) => g.name.toLowerCase().includes(term));
  }, [groups.data, search]);

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteRecipientGroup(removed.id);
      groups.mutate((current) => (current ?? []).filter((g) => g.id !== removed.id));
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
          <h1>Recipient groups</h1>
          <p className="muted">
            {groups.data
              ? `${groups.data.length} group${groups.data.length === 1 ? "" : "s"}`
              : " "}
            {groups.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setCreating(true)} disabled={!online}>
          Add group
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="group-search">
          Search groups
        </label>
        <input
          id="group-search"
          type="search"
          placeholder="Search by name"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {slow && groups.loading && <SlowNetworkNotice />}

      {groups.loading ? (
        <LoadingRows rows={3} columns={4} />
      ) : Boolean(groups.error) && !groups.data ? (
        <FailureState
          error={groups.error}
          online={online}
          onRetry={groups.reload}
          entity="recipient group"
        />
      ) : visible.length === 0 && search.trim() ? (
        <NoResultsPanel query={search.trim()} entity="groups" onClear={() => setSearch("")} />
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No recipient groups yet"
          icon="👥"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Add your first group
            </button>
          }
        >
          <p>
            A recipient group is who a notification policy tells when an incident opens —
            a site manager, an on-call rotation. Without one, a published policy has
            nobody to reach.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Recipient groups</caption>
            <thead>
              <tr>
                <th scope="col">Group</th>
                <th scope="col">Members</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((g) => (
                <tr key={g.id}>
                  <td>
                    <strong>{g.name}</strong>
                    {g.description && <div className="muted">{g.description}</div>}
                  </td>
                  <td>
                    {g.member_count} member{g.member_count === 1 ? "" : "s"}
                  </td>
                  <td>
                    <span className={`pill pill-${g.status}`}>{g.status}</span>
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setManaging(g)}
                      disabled={!online}
                    >
                      Members
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(g)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(g)}
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
        <GroupFormDialog
          group={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(group, wasNew) => {
            groups.mutate((current) => {
              const list = current ?? [];
              return wasNew
                ? [...list, group]
                : list.map((g) => (g.id === group.id ? group : g));
            });
            notify.success(
              wasNew ? `${group.name} was added` : `${group.name} was updated`,
              wasNew ? "Add members to it next." : undefined,
            );
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      {managing && (
        <MembersDialog
          group={managing}
          onClose={() => setManaging(null)}
          onMemberCountChanged={(count) =>
            groups.mutate((current) =>
              (current ?? []).map((g) =>
                g.id === managing.id ? { ...g, member_count: count } : g,
              ),
            )
          }
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this group?"
        busy={deleteBusy}
        confirmLabel="Remove group"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> and everyone in it will be removed.
            </p>
            <p className="muted">
              If a published policy still notifies this group, removal is refused until
              the group is taken out of that policy first — nobody is silently dropped
              from an escalation ladder still in effect.
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

function GroupFormDialog({
  group,
  onClose,
  onSaved,
}: {
  group: RecipientGroup | null;
  onClose: () => void;
  onSaved: (group: RecipientGroup, wasNew: boolean) => void;
}) {
  const isNew = group === null;
  const form = useForm({
    name: {
      initial: group?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(120, "Name")),
    },
    description: { initial: group?.description ?? "", label: "Description" },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const body = {
        name: form.values.name,
        description: form.values.description || undefined,
      };
      const saved = isNew
        ? await createRecipientGroup(body)
        : await updateRecipientGroup(group.id, body);
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={isNew ? "Add recipient group" : `Edit ${group.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("name")} label="Name" required placeholder="On call" />
        <Field
          {...form.field("description")}
          label="Description"
          hint="Optional — a note to remember who this reaches."
        />
        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Add group" : "Save changes"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}

/* --------------------------------------------------------------------- members */

function MembersDialog({
  group,
  onClose,
  onMemberCountChanged,
}: {
  group: RecipientGroup;
  onClose: () => void;
  onMemberCountChanged: (count: number) => void;
}) {
  const notify = useNotifications();
  const members = useResource(() => listMembers(group.id), [group.id]);
  const [slot, setSlot] = useState<RecipientMember | "new" | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);

  function afterChange(next: RecipientMember[]) {
    members.mutate(() => next);
    onMemberCountChanged(next.length);
  }

  async function handleRemove(member: RecipientMember) {
    setRemovingId(member.id);
    try {
      await removeMember(group.id, member.id);
      afterChange((members.data ?? []).filter((m) => m.id !== member.id));
      notify.success(`${member.display_name || member.email || "Recipient"} was removed`);
    } catch (err) {
      notify.error(
        "Could not remove that recipient",
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setRemovingId(null);
    }
  }

  return (
    <Dialog
      open
      title={`${group.name} — members`}
      onClose={onClose}
      footer={<button type="button" onClick={onClose}>Done</button>}
    >
      {members.loading ? (
        <InlineSpinner label="Loading members" />
      ) : (
        <>
          {(members.data ?? []).length === 0 && slot === null && (
            <p className="muted">
              Nobody is in this group yet — a policy that names it reaches no one.
            </p>
          )}

          {(members.data ?? []).length > 0 && (
            <ul className="member-list">
              {(members.data ?? []).map((m) => (
                <li key={m.id} className="member-row">
                  <div>
                    <strong>{m.display_name || m.email || m.phone_e164}</strong>
                    <div className="muted">
                      {[m.email, m.phone_e164].filter(Boolean).join(" · ") || "—"}
                    </div>
                    <div className="chip-row">
                      {m.channels.map((c) => (
                        <span key={c} className="pill">
                          {CHANNEL_LABELS[c] ?? c}
                        </span>
                      ))}
                    </div>
                    {m.active_schedule && (
                      <p className="muted" style={{ marginTop: 4 }}>
                        Quiet {m.active_schedule.start}–{m.active_schedule.end} (
                        {m.active_schedule.timezone}) — high and critical still reach them
                      </p>
                    )}
                  </div>
                  <div className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => setSlot(m)}>
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => void handleRemove(m)}
                      disabled={removingId === m.id}
                    >
                      {removingId === m.id ? "Removing…" : "Remove"}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {slot === null ? (
            <button type="button" className="btn-quiet" onClick={() => setSlot("new")}>
              Add member
            </button>
          ) : (
            <MemberForm
              groupId={group.id}
              member={slot === "new" ? null : slot}
              onCancel={() => setSlot(null)}
              onSaved={(saved, wasNew) => {
                const list = members.data ?? [];
                afterChange(
                  wasNew
                    ? [...list, saved]
                    : list.map((m) => (m.id === saved.id ? saved : m)),
                );
                notify.success(wasNew ? "Recipient added" : "Recipient updated");
                setSlot(null);
              }}
            />
          )}
        </>
      )}
    </Dialog>
  );
}

function MemberForm({
  groupId,
  member,
  onCancel,
  onSaved,
}: {
  groupId: string;
  member: RecipientMember | null;
  onCancel: () => void;
  onSaved: (member: RecipientMember, wasNew: boolean) => void;
}) {
  const isNew = member === null;
  const timezones = useResource(listTimezones, []);
  const [channels, setChannels] = useState<Channel[]>(member?.channels ?? ["email"]);
  const [quiet, setQuiet] = useState(member?.active_schedule !== null && member !== null);

  const form = useForm({
    display_name: { initial: member?.display_name ?? "", label: "Name" },
    email: { initial: member?.email ?? "", label: "Email" },
    phone_e164: { initial: member?.phone_e164 ?? "", label: "Phone" },
    quiet_start: { initial: member?.active_schedule?.start ?? "22:00", label: "Quiet from" },
    quiet_end: { initial: member?.active_schedule?.end ?? "06:00", label: "Quiet until" },
    quiet_timezone: {
      initial: member?.active_schedule?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
      label: "Timezone",
    },
  });

  function toggleChannel(c: Channel) {
    setChannels((current) =>
      current.includes(c) ? current.filter((x) => x !== c) : [...current, c],
    );
  }

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const schedule = quiet
        ? {
            start: form.values.quiet_start,
            end: form.values.quiet_end,
            timezone: form.values.quiet_timezone,
          }
        : null;
      const saved = isNew
        ? await addMember(groupId, {
            display_name: form.values.display_name || undefined,
            email: form.values.email || undefined,
            phone_e164: form.values.phone_e164 || undefined,
            channels,
            active_schedule: schedule,
          })
        : await updateMember(groupId, member.id, {
            display_name: form.values.display_name || undefined,
            email: form.values.email || undefined,
            phone_e164: form.values.phone_e164 || undefined,
            channels,
            active_schedule: schedule ?? undefined,
            clear_schedule: !quiet,
          });
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <form onSubmit={submit} noValidate className="member-form">
      <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

      <Field {...form.field("display_name")} label="Name" placeholder="Site manager" />
      <div className="field-row">
        <Field {...form.field("email")} label="Email" type="email" />
        <Field {...form.field("phone_e164")} label="Phone" placeholder="+919812345678" />
      </div>
      <p className="field-hint">Give an email or phone — at least one is required.</p>

      <div className="field">
        <label>Channels</label>
        <div className="chip-row">
          {CHANNELS.map((c) => (
            <label key={c} className="chip">
              <input
                type="checkbox"
                checked={channels.includes(c)}
                onChange={() => toggleChannel(c)}
              />
              {CHANNEL_LABELS[c]}
            </label>
          ))}
        </div>
      </div>

      <div className="field">
        <label>
          <input type="checkbox" checked={quiet} onChange={(e) => setQuiet(e.target.checked)} />
          {" "}Respect quiet hours for routine alerts
        </label>
        <p className="field-hint">
          High and critical incidents always reach this person regardless — this only
          holds back everything else until the window ends.
        </p>
      </div>

      {quiet && (
        <div className="field-row">
          <Field {...form.field("quiet_start")} label="Quiet from" type="time" />
          <Field {...form.field("quiet_end")} label="Quiet until" type="time" />
          <Field
            {...form.field("quiet_timezone")}
            label="Timezone"
            options={(timezones.data ?? [form.values.quiet_timezone]).map((tz) => ({
              value: tz,
              label: tz,
            }))}
          />
        </div>
      )}

      <FormActions
        submitting={form.submitting}
        submitLabel={isNew ? "Add recipient" : "Save changes"}
        onCancel={onCancel}
      />
    </form>
  );
}
