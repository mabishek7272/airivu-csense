import { useMemo, useState } from "react";
import type { Site, SiteInput } from "../api/sites";
import { createSite, deleteSite, listSites, listTimezones, updateSite } from "../api/sites";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import {
  ErrorSummary,
  Field,
  FormActions,
  combine,
  maxLength,
  onSubmitHandler,
  pattern,
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

/** Sites — the physical places everything else belongs to.
 *
 *  Most of this is bookkeeping. **Timezone is not.** Rule schedules are written in local
 *  time, and the pipeline converts using this value before evaluating, so a wrong zone
 *  produces no error anywhere — just an overnight rule that fires during the working day.
 *  The field is a picker over the server's own IANA list, not free text, and the form says
 *  what it is used for rather than leaving someone to guess it is cosmetic.
 */

export function SitesPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Site | null>(null);
  const [deleting, setDeleting] = useState<Site | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const sites = useResource(listSites, []);
  const slow = useSlowRequest(sites.loading);

  const visible = useMemo(() => {
    if (!sites.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return sites.data;
    return sites.data.filter(
      (s) =>
        s.name.toLowerCase().includes(term) ||
        s.code.toLowerCase().includes(term) ||
        (s.address?.city ?? "").toLowerCase().includes(term),
    );
  }, [sites.data, search]);

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteSite(removed.id);
      sites.mutate((current) => (current ?? []).filter((s) => s.id !== removed.id));
      notify.success(`${removed.name} was removed`);
      setDeleting(null);
    } catch (err) {
      // The server refuses when cameras are still attached, and its message names how
      // many. Surfacing that verbatim is more useful than a generic failure, because it
      // tells the person exactly what to do next.
      notify.error(
        `Could not remove ${removed.name}`,
        err instanceof Error ? err.message : undefined,
      );
      setDeleting(null);
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Sites</h1>
          <p className="muted">
            {sites.data ? `${sites.data.length} site${sites.data.length === 1 ? "" : "s"}` : " "}
            {sites.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setCreating(true)} disabled={!online}>
          Add site
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="site-search">
          Search sites
        </label>
        <input
          id="site-search"
          type="search"
          placeholder="Search by name, code or city"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {search && (
          <button type="button" className="btn-quiet" onClick={() => setSearch("")}>
            Clear
          </button>
        )}
      </div>

      {slow && sites.loading && <SlowNetworkNotice />}

      {sites.loading ? (
        <LoadingRows rows={3} columns={4} />
      ) : Boolean(sites.error) && !sites.data ? (
        <FailureState error={sites.error} online={online} onRetry={sites.reload} entity="site" />
      ) : visible.length === 0 && search.trim() ? (
        <NoResultsPanel query={search.trim()} entity="sites" onClear={() => setSearch("")} />
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No sites yet"
          icon="⌂"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Add your first site
            </button>
          }
        >
          <p>
            A site is a physical place — a warehouse, a depot, an office. Cameras and edge
            devices belong to one, and its timezone decides when time-based rules apply.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Sites</caption>
            <thead>
              <tr>
                <th scope="col">Site</th>
                <th scope="col">Timezone</th>
                <th scope="col">Attached</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((site) => (
                <tr key={site.id}>
                  <td>
                    <strong>{site.name}</strong>
                    <div className="muted mono">{site.code}</div>
                    {site.address && (
                      <div className="muted">
                        {[site.address.line1, site.address.city, site.address.country]
                          .filter(Boolean)
                          .join(", ")}
                      </div>
                    )}
                  </td>
                  <td>
                    {site.timezone}
                    <div className="muted">
                      {/* The current local time at the site, so a wrong zone is visible
                          at a glance rather than only when a rule misfires at 3am. */}
                      {localTimeAt(site.timezone)}
                    </div>
                  </td>
                  <td>
                    {site.camera_count} camera{site.camera_count === 1 ? "" : "s"}
                    <div className="muted">
                      {site.device_count} device{site.device_count === 1 ? "" : "s"}
                    </div>
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(site)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(site)}
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

      {Boolean(sites.error) && sites.data && (
        <div className="notice notice-warning" role="alert" style={{ marginTop: 16 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>This list may be out of date.</strong>
            <p>The last refresh failed. What you see was loaded earlier.</p>
          </div>
          <button type="button" className="btn-quiet" onClick={sites.reload}>
            Retry
          </button>
        </div>
      )}

      {(creating || editing) && (
        <SiteFormDialog
          site={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(site, wasNew) => {
            sites.mutate((current) => {
              const list = current ?? [];
              return wasNew ? [...list, site] : list.map((s) => (s.id === site.id ? site : s));
            });
            notify.success(
              wasNew ? `${site.name} was added` : `${site.name} was updated`,
              wasNew ? "You can add cameras and edge devices to it now." : undefined,
            );
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this site?"
        busy={deleteBusy}
        confirmLabel="Remove site"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> ({deleting?.code}) will be removed.
            </p>
            {deleting && (deleting.camera_count > 0 || deleting.device_count > 0) ? (
              // Said before the click, not after the server refuses. The action is left
              // enabled so the server stays the single authority, but nobody should be
              // surprised by the refusal.
              <div className="notice notice-warning">
                <span aria-hidden="true">⚠</span>
                <div>
                  <strong>This site still has things attached.</strong>
                  <p>
                    {deleting.camera_count} camera
                    {deleting.camera_count === 1 ? "" : "s"} and {deleting.device_count}{" "}
                    device{deleting.device_count === 1 ? "" : "s"}. Move or remove them
                    first — this will be refused otherwise.
                  </p>
                </div>
              </div>
            ) : (
              <p className="muted">
                Nothing is attached to it, so nothing else is affected.
              </p>
            )}
          </>
        }
        onConfirm={() => void handleDelete()}
        onCancel={() => setDeleting(null)}
      />
    </Layout>
  );
}

function localTimeAt(timezone: string): string {
  try {
    return `now ${new Intl.DateTimeFormat(undefined, {
      timeStyle: "short",
      timeZone: timezone,
    }).format(new Date())}`;
  } catch {
    return "unknown zone";
  }
}

/* ------------------------------------------------------------------ create / edit */

function SiteFormDialog({
  site,
  onClose,
  onSaved,
}: {
  site: Site | null;
  onClose: () => void;
  onSaved: (site: Site, wasNew: boolean) => void;
}) {
  const isNew = site === null;
  const timezones = useResource(listTimezones, []);

  const form = useForm({
    name: {
      initial: site?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(160, "Name")),
    },
    code: {
      initial: site?.code ?? "",
      label: "Code",
      validate: combine(
        required("Code"),
        pattern(
          /^[a-z0-9][a-z0-9_-]*$/,
          "Use lowercase letters, numbers, hyphens and underscores, starting with a letter or number.",
        ),
      ),
    },
    timezone: {
      initial: site?.timezone ?? guessTimezone(),
      label: "Timezone",
      validate: required("Timezone"),
    },
    line1: { initial: site?.address?.line1 ?? "", label: "Address" },
    city: { initial: site?.address?.city ?? "", label: "City" },
    country: { initial: site?.address?.country ?? "", label: "Country" },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const address = {
        line1: form.values.line1 || undefined,
        city: form.values.city || undefined,
        country: form.values.country || undefined,
      };
      const hasAddress = Object.values(address).some(Boolean);
      const body: SiteInput = {
        name: form.values.name,
        code: form.values.code,
        timezone: form.values.timezone,
        ...(hasAddress ? { address } : {}),
      };
      const saved = isNew
        ? await createSite(body)
        : await updateSite(site.id, body);
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  const zoneOptions = (timezones.data ?? [form.values.timezone]).map((tz) => ({
    value: tz,
    label: tz,
  }));

  return (
    <Dialog open title={isNew ? "Add site" : `Edit ${site.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field {...form.field("name")} label="Name" required placeholder="Chennai Depot" />
        <Field
          {...form.field("code")}
          label="Code"
          required
          hint="A short identifier, unique within your account."
          placeholder="chennai-depot"
        />
        <Field
          {...form.field("timezone")}
          label="Timezone"
          required
          options={zoneOptions}
          // Stated because it is not cosmetic: a wrong zone produces no error, just rules
          // that fire at the wrong hour.
          hint={
            timezones.loading
              ? "Loading zones…"
              : "Time-based rules are evaluated in this zone. An overnight rule on a site set to the wrong zone will fire during the working day."
          }
        />

        <Field {...form.field("line1")} label="Address" placeholder="12 Anna Salai" />
        <div className="field-row">
          <Field {...form.field("city")} label="City" />
          <Field {...form.field("country")} label="Country" />
        </div>

        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Add site" : "Save changes"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}

/** The browser's own zone as a starting point — right far more often than UTC, and the
 *  user can change it. */
function guessTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
