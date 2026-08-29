import { useMemo, useState } from "react";
import { listSites } from "../api/sites";
import type { Zone, ZoneInput, ZonePoint } from "../api/zones";
import { createZone, deleteZone, listZones, updateZone } from "../api/zones";
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
import { PolygonEditor, areaOf } from "../components/PolygonEditor";
import type { Point } from "../components/PolygonEditor";
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

/** Zones — where a rule applies.
 *
 *  The listing leads with two numbers that are usually invisible until something has gone
 *  wrong: how much of the frame the zone covers, and how many rules point at it. The first
 *  answers "why does this never fire" — almost always a zone far smaller than its author
 *  believed. The second is what makes redrawing a boundary a considered act rather than a
 *  drag.
 */

const ZONE_TYPES = [
  { value: "restricted", label: "Restricted — nobody should be here" },
  { value: "safety", label: "Safety — PPE or hazard area" },
  { value: "exclusion", label: "Exclusion — ignore detections here" },
  { value: "counting", label: "Counting — measure throughput" },
  { value: "general", label: "General" },
];

const MIN_POINTS = 3;
// Matches the server's MIN_AREA, so the form refuses what the API would refuse.
const MIN_AREA = 0.0005;

export function ZonesPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [siteFilter, setSiteFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Zone | null>(null);
  const [deleting, setDeleting] = useState<Zone | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const zones = useResource(
    () => listZones({ site_id: siteFilter || undefined }),
    [siteFilter],
  );
  const sites = useResource(listSites, []);
  const slow = useSlowRequest(zones.loading);

  const visible = useMemo(() => {
    if (!zones.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return zones.data;
    return zones.data.filter(
      (z) =>
        z.name.toLowerCase().includes(term) ||
        z.zone_type.toLowerCase().includes(term) ||
        (z.site_name ?? "").toLowerCase().includes(term),
    );
  }, [zones.data, search]);

  const hasFilters = search.trim() !== "" || siteFilter !== "";

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteZone(removed.id);
      zones.mutate((current) => (current ?? []).filter((z) => z.id !== removed.id));
      notify.success(`${removed.name} was removed`);
      setDeleting(null);
    } catch (err) {
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
          <h1>Zones</h1>
          <p className="muted">
            {zones.data ? `${zones.data.length} zone${zones.data.length === 1 ? "" : "s"}` : " "}
            {zones.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating(true)}
          disabled={!online || (sites.data?.length ?? 0) === 0}
        >
          Add zone
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="zone-search">
          Search zones
        </label>
        <input
          id="zone-search"
          type="search"
          placeholder="Search by name, type or site"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="zone-site">
          Filter by site
        </label>
        <select id="zone-site" value={siteFilter} onChange={(e) => setSiteFilter(e.target.value)}>
          <option value="">All sites</option>
          {(sites.data ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        {hasFilters && (
          <button
            type="button"
            className="btn-quiet"
            onClick={() => {
              setSearch("");
              setSiteFilter("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {slow && zones.loading && <SlowNetworkNotice />}

      {zones.loading ? (
        <LoadingRows rows={3} columns={4} />
      ) : Boolean(zones.error) && !zones.data ? (
        <FailureState error={zones.error} online={online} onRetry={zones.reload} entity="zone" />
      ) : visible.length === 0 && hasFilters ? (
        <NoResultsPanel
          query={search.trim() || undefined}
          entity="zones"
          onClear={() => {
            setSearch("");
            setSiteFilter("");
          }}
        />
      ) : (sites.data?.length ?? 0) === 0 ? (
        <EmptyPanel title="Add a site first" icon="⌂">
          <p>
            A zone is drawn against a camera at a site, so there needs to be a site before
            there can be a zone.
          </p>
        </EmptyPanel>
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No zones yet"
          icon="⬠"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Draw your first zone
            </button>
          }
        >
          <p>
            A zone is the area a rule watches — the restricted dock, the space in front of
            a fire exit. Without one, a rule applies to the whole frame.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Zones</caption>
            <thead>
              <tr>
                <th scope="col">Zone</th>
                <th scope="col">Shape</th>
                <th scope="col">Used by</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((zone) => (
                <tr key={zone.id}>
                  <td>
                    <strong>{zone.name}</strong>
                    <div className="muted">{zone.zone_type}</div>
                    {zone.site_name && <div className="muted">{zone.site_name}</div>}
                  </td>
                  <td>
                    <ZoneThumbnail polygon={zone.polygon} />
                  </td>
                  <td>
                    {/* Surfaced because a zone covering 2% of the frame is the usual
                        answer to "why does this rule never fire". */}
                    {(zone.area_fraction * 100).toFixed(1)}% of frame
                    <div className="muted">
                      {zone.polygon.length} points ·{" "}
                      {zone.rule_count === 0 ? (
                        "no rules"
                      ) : (
                        <strong>
                          {zone.rule_count} rule{zone.rule_count === 1 ? "" : "s"}
                        </strong>
                      )}
                    </div>
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(zone)}
                      disabled={!online}
                    >
                      Redraw
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(zone)}
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

      {Boolean(zones.error) && zones.data && (
        <div className="notice notice-warning" role="alert" style={{ marginTop: 16 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>This list may be out of date.</strong>
            <p>The last refresh failed. What you see was loaded earlier.</p>
          </div>
          <button type="button" className="btn-quiet" onClick={zones.reload}>
            Retry
          </button>
        </div>
      )}

      {(creating || editing) && (
        <ZoneFormDialog
          zone={editing}
          sites={(sites.data ?? []).map((s) => ({ value: s.id, label: s.name }))}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(zone, wasNew) => {
            zones.mutate((current) => {
              const list = current ?? [];
              return wasNew ? [...list, zone] : list.map((z) => (z.id === zone.id ? zone : z));
            });
            notify.success(
              wasNew ? `${zone.name} was created` : `${zone.name} was redrawn`,
              !wasNew && zone.rule_count > 0
                ? `${zone.rule_count} rule${zone.rule_count === 1 ? "" : "s"} now use the new boundary.`
                : undefined,
            );
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this zone?"
        busy={deleteBusy}
        confirmLabel="Remove zone"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> will be removed.
            </p>
            {deleting && deleting.rule_count > 0 ? (
              // The sharp case, and worth spelling out: removing a zone does not disable
              // the rules using it - it widens them to the whole frame.
              <div className="notice notice-warning">
                <span aria-hidden="true">⚠</span>
                <div>
                  <strong>
                    {deleting.rule_count} rule{deleting.rule_count === 1 ? "" : "s"} use this
                    zone.
                  </strong>
                  <p>
                    Removing it would not disable them — a rule with no zone watches the
                    whole frame, so they would start firing on everything the camera sees.
                    This will be refused until they are changed.
                  </p>
                </div>
              </div>
            ) : (
              <p className="muted">No rules use it, so nothing else changes.</p>
            )}
          </>
        }
        onConfirm={() => void handleDelete()}
        onCancel={() => setDeleting(null)}
      />
    </Layout>
  );
}

/** A small read-only rendering, so the list shows shapes rather than point counts. */
function ZoneThumbnail({ polygon }: { polygon: ZonePoint[] }) {
  if (polygon.length < 3) return <span className="muted">No shape</span>;
  return (
    <svg className="zone-thumb" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
      <rect x="0" y="0" width="1" height="1" className="zone-thumb-frame" />
      <polygon points={polygon.map(([x, y]) => `${x},${y}`).join(" ")} className="polygon-shape" />
    </svg>
  );
}

/* ------------------------------------------------------------------ create / edit */

function ZoneFormDialog({
  zone,
  sites,
  onClose,
  onSaved,
}: {
  zone: Zone | null;
  sites: { value: string; label: string }[];
  onClose: () => void;
  onSaved: (zone: Zone, wasNew: boolean) => void;
}) {
  const isNew = zone === null;
  const [polygon, setPolygon] = useState<Point[]>(
    (zone?.polygon as Point[]) ?? [],
  );
  const [polygonTouched, setPolygonTouched] = useState(false);

  const form = useForm({
    site_id: {
      initial: zone?.site_id ?? sites[0]?.value ?? "",
      label: "Site",
      validate: required("Site"),
    },
    name: {
      initial: zone?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(120, "Name")),
    },
    zone_type: { initial: zone?.zone_type ?? "restricted", label: "Type" },
  });

  // The polygon lives outside useForm because it is not a text field, so its validation
  // is expressed here — same rules the server applies, so the form never submits
  // something the API will reject.
  const polygonError =
    polygon.length < MIN_POINTS
      ? `Draw at least ${MIN_POINTS} points to enclose an area.`
      : areaOf(polygon) < MIN_AREA
        ? "This shape encloses almost no area, so no detection would ever overlap it enough to trigger a rule."
        : undefined;

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    setPolygonTouched(true);
    if (polygonError) {
      document.querySelector<HTMLElement>(".error-summary")?.focus();
      return;
    }
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const body: ZoneInput = {
        site_id: form.values.site_id,
        name: form.values.name,
        zone_type: form.values.zone_type,
        polygon,
      };
      const saved = isNew ? await createZone(body) : await updateZone(zone.id, body);
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  const allErrors = [
    ...form.visibleErrors,
    ...(polygonError && (polygonTouched || form.submitAttempted)
      ? [{ name: "polygon", label: "Boundary", error: polygonError }]
      : []),
  ];

  return (
    <Dialog open title={isNew ? "Draw a zone" : `Redraw ${zone.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={allErrors} formError={form.formError} />

        {!isNew && zone.rule_count > 0 && (
          <div className="notice notice-warning" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>
                {zone.rule_count} rule{zone.rule_count === 1 ? "" : "s"} use this zone.
              </strong>
              <p>
                Changing the boundary changes what they fire on. Widening it can produce
                constant alerts; narrowing it can stop an intrusion being detected.
              </p>
            </div>
          </div>
        )}

        <Field {...form.field("name")} label="Name" required placeholder="Restricted dock" />
        <Field {...form.field("site_id")} label="Site" required options={sites} />
        <Field {...form.field("zone_type")} label="Type" options={ZONE_TYPES} />

        <div className={`field${polygonError && polygonTouched ? " field-invalid" : ""}`}>
          <label id="polygon-label">Boundary</label>
          <PolygonEditor
            points={polygon}
            onChange={(next) => {
              setPolygon(next);
              setPolygonTouched(true);
            }}
          />
          {polygonError && (polygonTouched || form.submitAttempted) && (
            <p className="field-error">
              <span aria-hidden="true">✕ </span>
              {polygonError}
            </p>
          )}
        </div>

        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Create zone" : "Save boundary"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}
