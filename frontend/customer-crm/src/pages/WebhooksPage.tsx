import { useEffect, useMemo, useState } from "react";
import type { WebhookDelivery, WebhookEndpoint } from "../api/webhooks";
import {
  createWebhook,
  deleteWebhook,
  listWebhookDeliveries,
  listWebhooks,
  rotateWebhookSecret,
  testWebhook,
  updateWebhook,
} from "../api/webhooks";
import { DeliveryStatusBadge } from "../components/Badges";
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
import { Layout, relativeTime } from "../components/Layout";
import { useNotifications } from "../components/Notifications";
import {
  EmptyPanel,
  ErrorPanel,
  FailureState,
  InlineSpinner,
  LoadingRows,
  NoResultsPanel,
  SlowNetworkNotice,
} from "../components/States";
import { useOnlineStatus, useSlowRequest } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Webhook endpoint management: the create/edit/rotate/test/delete/delivery-history cycle
 *  for the tenant-owned HTTP receivers `backend/tenant_api/app/api/webhooks.py` exposes.
 *  Until now these were API- and `scripts/e2e_webhooks.py`-only (CHECKLIST.md named the
 *  gap explicitly) — this is that page.
 *
 *  **Two things this page is careful about, both inherited from `CamerasPage.tsx`.**
 *
 *  Empty and no-results stay distinct screens: "you have no webhooks yet" offers a button
 *  that adds one, "no webhooks match your filters" offers a button that clears them.
 *
 *  **The signing secret is write-only, and the UI says so out loud.** The API returns it
 *  exactly twice — the moment a webhook is created, and the moment its secret is rotated —
 *  and never again, not even to the person who just set it. There is no field anywhere on
 *  this page that renders a stored secret, because there is no way to ask the server for
 *  one back. Both moments route through the same `SecretRevealDialog`, which mirrors
 *  `EdgePage.tsx`'s own `TokenDialog` for exactly the same reason: a value that genuinely
 *  cannot be retrieved again needs the interface to say so before the dialog is closed,
 *  with a copy button, not after.
 *
 *  **Delivery history is a dialog, not a route.** `NotificationPoliciesPage.tsx`'s own
 *  `EscalationDialog` is the closest precedent in this app for "manage a per-row
 *  sub-resource without leaving the list" — the same shape fits here, and a dedicated
 *  route would need its own loading/empty/error handling for what is, underneath, one
 *  more filtered, cursor-paginated list. Pagination follows `AuditPage.tsx`'s own
 *  "Load more" convention against the real cursor the API returns — not client-side
 *  paging of a single fetched page.
 */

const WEBHOOK_STATUS_PILL: Record<string, string> = {
  active: "pill-ready",
  disabled: "pill-disabled",
};

function parseEventFilters(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function describeEventFilters(filters: string[]): string {
  return filters.length === 0 ? "All events" : filters.join(", ");
}

export function WebhooksPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<WebhookEndpoint | null>(null);
  const [rotating, setRotating] = useState<WebhookEndpoint | null>(null);
  const [rotateBusy, setRotateBusy] = useState(false);
  const [deleting, setDeleting] = useState<WebhookEndpoint | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [viewingDeliveriesFor, setViewingDeliveriesFor] = useState<WebhookEndpoint | null>(null);
  const [revealing, setRevealing] = useState<{
    webhookName: string;
    secret: string;
    justRotated: boolean;
  } | null>(null);

  const webhooks = useResource(listWebhooks, []);
  const slow = useSlowRequest(webhooks.loading);

  // Filtering client-side, same call `CamerasPage.tsx` makes for the same reason: the
  // list is small and the feedback is instant. If it grows past a page this moves to the
  // server, and the no-results state is already the right shape for that.
  const visible = useMemo(() => {
    if (!webhooks.data) return [];
    const term = search.trim().toLowerCase();
    return webhooks.data.filter((w) => {
      if (statusFilter && w.status !== statusFilter) return false;
      if (!term) return true;
      return (
        w.name.toLowerCase().includes(term) ||
        w.url_host_display.toLowerCase().includes(term) ||
        w.event_filters.some((f) => f.toLowerCase().includes(term))
      );
    });
  }, [webhooks.data, search, statusFilter]);

  const hasFilters = search.trim() !== "" || statusFilter !== "";

  function clearFilters() {
    setSearch("");
    setStatusFilter("");
  }

  async function handleTest(webhook: WebhookEndpoint) {
    setTestingId(webhook.id);
    try {
      // A real, live HTTP POST to the tenant's own configured destination — not a dry
      // run — so both a success and a failure are genuine findings, not app errors.
      const result = await testWebhook(webhook.id);
      if (result.delivered) {
        notify.success(
          `${webhook.name} answered`,
          `HTTP ${result.response_status}` +
            (result.response_time_ms != null ? ` in ${result.response_time_ms}ms` : ""),
        );
      } else {
        notify.notify({
          kind: "warning",
          title: `${webhook.name} did not deliver`,
          detail:
            result.error ??
            (result.response_status != null
              ? `The endpoint responded HTTP ${result.response_status}.`
              : "No response was recorded before the request gave up."),
        });
      }
    } catch (err) {
      notify.error(`Could not test ${webhook.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setTestingId(null);
    }
  }

  async function handleRotate() {
    if (!rotating) return;
    setRotateBusy(true);
    const webhook = rotating;
    try {
      const result = await rotateWebhookSecret(webhook.id);
      setRotating(null);
      setRevealing({ webhookName: webhook.name, secret: result.signing_secret, justRotated: true });
    } catch (err) {
      notify.error(
        `Could not rotate the secret for ${webhook.name}`,
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setRotateBusy(false);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteWebhook(removed.id);
      webhooks.mutate((current) => (current ?? []).filter((w) => w.id !== removed.id));
      notify.success(`${removed.name} was removed`, "Its stored URL and signing secret were destroyed.");
      setDeleting(null);
    } catch (err) {
      notify.error(`Could not remove ${removed.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Webhooks</h1>
          <p className="muted">
            {webhooks.data
              ? `${webhooks.data.length} endpoint${webhooks.data.length === 1 ? "" : "s"}`
              : " "}
            {webhooks.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setCreating(true)} disabled={!online}>
          Add webhook
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="webhook-search">
          Search webhooks
        </label>
        <input
          id="webhook-search"
          type="search"
          placeholder="Search by name, host or event"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="webhook-status">
          Filter by status
        </label>
        <select id="webhook-status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">Any status</option>
          <option value="active">Active</option>
          <option value="disabled">Disabled</option>
        </select>
        {hasFilters && (
          <button type="button" className="btn-quiet" onClick={clearFilters}>
            Clear
          </button>
        )}
      </div>

      {slow && webhooks.loading && <SlowNetworkNotice />}

      {/* Order matters: loading is checked before empty, so a slow request never reads
          as "you have no webhooks". */}
      {webhooks.loading ? (
        <LoadingRows rows={4} columns={6} />
      ) : Boolean(webhooks.error) && !webhooks.data ? (
        <FailureState error={webhooks.error} online={online} onRetry={webhooks.reload} entity="webhook" />
      ) : visible.length === 0 && hasFilters ? (
        <NoResultsPanel query={search.trim() || undefined} entity="webhooks" onClear={clearFilters} />
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No webhooks yet"
          icon="⇄"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Add your first webhook
            </button>
          }
        >
          <p>
            A webhook lets another system receive CSense events as signed HTTP POSTs — an
            incident being acknowledged, for example. Add one and point it at your own
            HTTPS endpoint; you can send it a real test delivery right after.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Webhook endpoints</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Target</th>
                <th scope="col">Events</th>
                <th scope="col">Status</th>
                <th scope="col">Created</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((webhook) => (
                <tr key={webhook.id}>
                  <td>
                    <strong>{webhook.name}</strong>
                  </td>
                  {/* Only the hostname — the API never returns the path or query, which
                      some receivers embed their own auth token in. There is nothing more
                      to reconstruct here; this is all the server ever hands back. */}
                  <td className="mono">{webhook.url_host_display}</td>
                  <td>{describeEventFilters(webhook.event_filters)}</td>
                  <td>
                    <span className={`pill ${WEBHOOK_STATUS_PILL[webhook.status] ?? ""}`}>
                      {webhook.status}
                    </span>
                  </td>
                  <td title={webhook.created_at}>{relativeTime(webhook.created_at)}</td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setViewingDeliveriesFor(webhook)}
                      disabled={!online}
                    >
                      Deliveries
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => void handleTest(webhook)}
                      disabled={!online || testingId === webhook.id}
                    >
                      {testingId === webhook.id ? "Testing…" : "Test"}
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setRotating(webhook)}
                      disabled={!online}
                    >
                      Rotate secret
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(webhook)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(webhook)}
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

      {/* A refresh that failed while data is still on screen: show both, so the list
          stays usable and the staleness is admitted rather than hidden. */}
      {Boolean(webhooks.error) && webhooks.data && (
        <div className="notice notice-warning" role="alert" style={{ marginTop: 16 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>This list may be out of date.</strong>
            <p>The last refresh failed. What you see was loaded earlier.</p>
          </div>
          <button type="button" className="btn-quiet" onClick={webhooks.reload}>
            Retry
          </button>
        </div>
      )}

      {(creating || editing) && (
        <WebhookFormDialog
          webhook={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onCreated={(webhook, secret) => {
            webhooks.mutate((current) => [...(current ?? []), webhook]);
            setCreating(false);
            setRevealing({ webhookName: webhook.name, secret, justRotated: false });
          }}
          onUpdated={(webhook) => {
            webhooks.mutate((current) => (current ?? []).map((w) => (w.id === webhook.id ? webhook : w)));
            notify.success(`${webhook.name} was updated`);
            setEditing(null);
          }}
        />
      )}

      {viewingDeliveriesFor && (
        <DeliveryHistoryDialog webhook={viewingDeliveriesFor} onClose={() => setViewingDeliveriesFor(null)} />
      )}

      {revealing && (
        <SecretRevealDialog
          webhookName={revealing.webhookName}
          secret={revealing.secret}
          justRotated={revealing.justRotated}
          onClose={() => setRevealing(null)}
        />
      )}

      <ConfirmDialog
        open={rotating !== null}
        title="Rotate this signing secret?"
        busy={rotateBusy}
        confirmLabel="Rotate secret"
        body={
          <>
            <p>
              <strong>{rotating?.name}</strong> will be issued a brand new signing secret.
            </p>
            <p className="muted">
              The old secret stops verifying signatures immediately. Anything still checking
              deliveries against it will start rejecting them until it is updated with the
              new one, shown once right after this.
            </p>
          </>
        }
        onConfirm={() => void handleRotate()}
        onCancel={() => setRotating(null)}
      />

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this webhook?"
        busy={deleteBusy}
        confirmLabel="Remove webhook"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> ({deleting?.url_host_display}) will stop
              receiving events immediately.
            </p>
            <p className="muted">
              Its stored URL and signing secret are destroyed at the same time, and its
              delivery history goes with it.
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

function WebhookFormDialog({
  webhook,
  onClose,
  onCreated,
  onUpdated,
}: {
  webhook: WebhookEndpoint | null;
  onClose: () => void;
  onCreated: (webhook: WebhookEndpoint, secret: string) => void;
  onUpdated: (webhook: WebhookEndpoint) => void;
}) {
  const isNew = webhook === null;
  const [eventFiltersRaw, setEventFiltersRaw] = useState(webhook?.event_filters.join(", ") ?? "");
  const [status, setStatus] = useState(webhook?.status ?? "active");

  const form = useForm({
    name: {
      initial: webhook?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(200, "Name")),
    },
    url: {
      initial: "",
      label: "URL",
      // Mirrors `_validate_and_split_url`'s own message in webhooks.py exactly, so a
      // rejection reads the same whether it was caught here or by the server.
      validate: isNew
        ? combine(required("URL"), (value) => {
            let parsed: URL;
            try {
              parsed = new URL(value);
            } catch {
              return "Webhook URLs must be https:// with a real hostname.";
            }
            return parsed.protocol === "https:" && parsed.hostname
              ? undefined
              : "Webhook URLs must be https:// with a real hostname.";
          })
        : undefined,
    },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const eventFilters = parseEventFilters(eventFiltersRaw);
      if (isNew) {
        const created = await createWebhook({
          name: form.values.name,
          url: form.values.url,
          event_filters: eventFilters,
        });
        const { signing_secret, ...rest } = created;
        onCreated(rest, signing_secret);
      } else {
        const updated = await updateWebhook(webhook.id, {
          name: form.values.name,
          event_filters: eventFilters,
          status: status as "active" | "disabled",
        });
        onUpdated(updated);
      }
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={isNew ? "Add webhook" : `Edit ${webhook.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field {...form.field("name")} label="Name" required placeholder="Incident feed" />

        {isNew ? (
          <Field
            {...form.field("url")}
            label="URL"
            required
            placeholder="https://example.com/hooks/csense"
            hint="Must be https://. This is write-only: once saved, no request will ever show it back to you — not even this one."
          />
        ) : (
          <div className="notice notice-info" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">ⓘ</span>
            <div>
              <strong>The destination URL cannot be edited here.</strong>
              <p>
                Changing where events go is a new endpoint, not a change to this one — add
                one and remove this one instead. Its current target is{" "}
                <span className="mono">{webhook.url_host_display}</span>.
              </p>
            </div>
          </div>
        )}

        <div className="field">
          <label htmlFor="webhook-event-filters">Event filters</label>
          <p className="field-hint" id="webhook-event-filters-hint">
            Comma-separated event types, e.g. <span className="mono">incident.acknowledged.v1</span>.
            Leave blank to receive every event.
          </p>
          <input
            id="webhook-event-filters"
            aria-describedby="webhook-event-filters-hint"
            value={eventFiltersRaw}
            onChange={(e) => setEventFiltersRaw(e.target.value)}
            placeholder="incident.acknowledged.v1, incident.dismissed.v1"
          />
        </div>

        {!isNew && (
          <div className="field">
            <label htmlFor="webhook-status">Status</label>
            <p className="field-hint" id="webhook-status-hint">
              A disabled endpoint is skipped by automatic delivery entirely.
            </p>
            <select
              id="webhook-status"
              aria-describedby="webhook-status-hint"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              <option value="active">Active</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>
        )}

        <FormActions submitting={form.submitting} submitLabel={isNew ? "Add webhook" : "Save changes"} onCancel={onClose} />
      </form>
    </Dialog>
  );
}

/* --------------------------------------------------------------- secret reveal */

/** Shows a signing secret exactly once, the same shape `EdgePage.tsx`'s own `TokenDialog`
 *  uses for an enrolment token: a plain warning that this is the only chance, a copy
 *  button with a clipboard fallback, and nothing that persists the value anywhere this
 *  component itself doesn't control. Shared by both create and rotate-secret, since both
 *  produce exactly the same kind of value under exactly the same constraint. */
function SecretRevealDialog({
  webhookName,
  secret,
  justRotated,
  onClose,
}: {
  webhookName: string;
  secret: string;
  justRotated: boolean;
  onClose: () => void;
}) {
  const notify = useNotifications();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      // Clipboard access is denied in some contexts (no permission, insecure origin).
      // Say so rather than appearing to succeed — the user needs to know to select the
      // value by hand instead.
      notify.notify({
        kind: "warning",
        title: "Could not copy automatically",
        detail: "Select the secret and copy it manually.",
      });
    }
  }

  return (
    <Dialog
      open
      title={justRotated ? `New signing secret for ${webhookName}` : `Signing secret for ${webhookName}`}
      onClose={onClose}
      footer={
        <button type="button" onClick={onClose}>
          Done
        </button>
      }
    >
      <div className="notice notice-warning" style={{ marginBottom: 16 }}>
        <span aria-hidden="true">⚠</span>
        <div>
          <strong>This is shown once.</strong>
          <p>
            It is not stored anywhere it can be read back — not on this page, not by any
            API request. Copy it into whatever verifies deliveries now; if you lose it, the
            only recovery is rotating to a new one, which invalidates this one.
          </p>
        </div>
      </div>

      <div className="token-display">
        <code className="mono">{secret}</code>
        <button type="button" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      {justRotated && (
        <p className="muted" style={{ marginTop: 12 }}>
          The previous secret stopped verifying the moment this one was issued.
        </p>
      )}
    </Dialog>
  );
}

/* ---------------------------------------------------------------- delivery history */

const DELIVERY_PAGE_SIZE = 25;

/** Real delivery history for one webhook, cursor-paginated against the real
 *  `GET /{id}/deliveries` endpoint — "Load more" follows a real `next_cursor`, the same
 *  convention `AuditPage.tsx` already established, not client-side paging of one fetched
 *  page. Rendered as a card list rather than a wide table: this dialog is the app's
 *  standard 560px width, and a table with seven columns of delivery detail would either
 *  force a horizontal scrollbar or crush every column unreadably — a card per delivery
 *  reads better at this width and still shows every field. */
function DeliveryHistoryDialog({ webhook, onClose }: { webhook: WebhookEndpoint; onClose: () => void }) {
  const [items, setItems] = useState<WebhookDelivery[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function load(reset: boolean, filter: string) {
    if (reset) {
      setLoading(true);
      setError(null);
    } else {
      setLoadingMore(true);
    }
    try {
      const page = await listWebhookDeliveries(webhook.id, {
        status: filter || undefined,
        cursor: reset ? undefined : nextCursor ?? undefined,
        limit: DELIVERY_PAGE_SIZE,
      });
      setItems((current) => (reset ? page.items : [...(current ?? []), ...page.items]));
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }

  // Runs once on open, and again whenever the status filter changes — mirroring
  // `AuditPage.tsx`'s own effect exactly, including resetting to a fresh first page
  // rather than trying to reconcile a filter change against an already-loaded tail.
  useEffect(() => {
    void load(true, statusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  return (
    <Dialog open title={`Deliveries for ${webhook.name}`} onClose={onClose} footer={<button type="button" onClick={onClose}>Close</button>}>
      <div className="filter-bar" style={{ marginBottom: 16 }}>
        <label className="visually-hidden" htmlFor="delivery-status-filter">
          Filter by delivery status
        </label>
        <select
          id="delivery-status-filter"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">Any status</option>
          <option value="pending">Pending</option>
          <option value="succeeded">Succeeded</option>
          <option value="failed">Failed</option>
          <option value="abandoned">Abandoned</option>
        </select>
      </div>

      {loading ? (
        <InlineSpinner label="Loading deliveries" />
      ) : error ? (
        <ErrorPanel
          message={error instanceof Error ? error.message : "Could not load delivery history."}
          onRetry={() => void load(true, statusFilter)}
        />
      ) : items && items.length === 0 ? (
        <p className="muted">
          {statusFilter
            ? "No deliveries match this filter yet."
            : "No deliveries recorded yet. Automatic delivery fires when a matching event occurs; \"Test\" on the list above sends one immediately."}
        </p>
      ) : (
        <>
          <div className="card-list">
            {(items ?? []).map((delivery) => (
              <div key={delivery.id} className="card" style={{ padding: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <span className="mono">{delivery.event_type}</span>
                  <DeliveryStatusBadge status={delivery.status} />
                </div>
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  Attempt {delivery.attempt_number} · scheduled {relativeTime(delivery.scheduled_at)}
                  {delivery.sent_at ? ` · sent ${relativeTime(delivery.sent_at)}` : ""}
                </div>
                {delivery.response_status != null && (
                  <div style={{ fontSize: 13, marginTop: 4 }}>
                    HTTP {delivery.response_status}
                    {delivery.response_time_ms != null ? ` in ${delivery.response_time_ms}ms` : ""}
                  </div>
                )}
                {delivery.failure_summary_redacted && (
                  <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                    {delivery.failure_summary_redacted}
                  </div>
                )}
                {delivery.next_attempt_at && (
                  <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                    Next attempt {relativeTime(delivery.next_attempt_at)}
                  </div>
                )}
              </div>
            ))}
          </div>
          {nextCursor && (
            <button
              type="button"
              className="btn-quiet"
              style={{ marginTop: 12 }}
              disabled={loadingMore}
              onClick={() => void load(false, statusFilter)}
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
        </>
      )}
    </Dialog>
  );
}
