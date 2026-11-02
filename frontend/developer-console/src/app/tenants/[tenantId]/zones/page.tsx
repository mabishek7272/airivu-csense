"use client";

import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import type { Site, Zone } from "@/api/tenantInfra";
import { createZone, deleteZone, listSites, listZones } from "@/api/tenantInfra";
import { ConfirmDialog, Dialog } from "@/components/Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, required, useForm } from "@/components/Form";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

const ZONE_TYPES = ["general", "restricted", "safety", "exclusion", "counting"];
const PRIVACY_LEVELS = ["standard", "sensitive", "high"];
const FULL_FRAME_POLYGON = "[[0.05,0.05],[0.95,0.05],[0.95,0.95],[0.05,0.95]]";

export default function TenantZonesPage({ params }: { params: { tenantId: string } }) {
  const { tenantId } = params;
  const { isAuthenticated } = useAuth();
  const online = useOnlineStatus();
  const notify = useNotifications();

  const zones = useResource(() => listZones(tenantId), [isAuthenticated, tenantId]);
  const sites = useResource(() => listSites(tenantId), [isAuthenticated, tenantId]);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Zone | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await deleteZone(tenantId, deleting.id);
      zones.mutate((current) => (current ?? []).filter((z) => z.id !== deleting.id));
      notify.success(`${deleting.name} removed`);
      setDeleting(null);
    } catch (err) {
      notify.error(`Could not remove ${deleting.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Zones</h2>
          <p>{zones.data ? `${zones.data.length} zone${zones.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online || (sites.data ?? []).length === 0} onClick={() => setCreating(true)}>
          Add zone
        </button>
      </div>

      {zones.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : zones.error && !zones.data ? (
        <FailureState error={zones.error} online={online} onRetry={zones.reload} entity="zones" />
      ) : zones.data && zones.data.length === 0 ? (
        <EmptyPanel title="No zones yet" icon="○">
          <p>A zone limits where a rule applies. Add a site first if none exists.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Zones</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Site</th>
                <th scope="col">Type</th>
                <th scope="col">Coverage</th>
                <th scope="col">Rules</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(zones.data ?? []).map((zone) => (
                <tr key={zone.id}>
                  <td>{zone.name}</td>
                  <td>{zone.site_name ?? "—"}</td>
                  <td>{zone.zone_type}</td>
                  <td>{(zone.area_fraction * 100).toFixed(1)}%</td>
                  <td>{zone.rule_count}</td>
                  <td className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => setDeleting(zone)} disabled={!online}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating && (
        <CreateZoneDialog
          tenantId={tenantId}
          sites={sites.data ?? []}
          onClose={() => setCreating(false)}
          onCreated={(zone) => {
            zones.mutate((current) => [...(current ?? []), zone]);
            notify.success(`${zone.name} added`);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={`Remove ${deleting?.name ?? ""}`}
        body={<p>Any rule pointing at this zone will fall back to watching the whole frame.</p>}
        confirmLabel="Remove"
        busy={deleteBusy}
        onConfirm={handleDelete}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}

function CreateZoneDialog({
  tenantId,
  sites,
  onClose,
  onCreated,
}: {
  tenantId: string;
  sites: Site[];
  onClose: () => void;
  onCreated: (zone: Zone) => void;
}) {
  const [submitting, setSubmitting] = useState(false);

  const form = useForm({
    site_id: { initial: sites[0]?.id ?? "", label: "Site", validate: required("Site") },
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(120, "Name")) },
    zone_type: { initial: "restricted", label: "Type" },
    privacy_level: { initial: "standard", label: "Privacy level" },
    polygon: {
      initial: FULL_FRAME_POLYGON,
      label: "Polygon",
      validate: (value: string) => {
        try {
          const points = JSON.parse(value);
          if (!Array.isArray(points) || points.length < 3) return "Needs at least 3 [x, y] points.";
        } catch {
          return "Must be valid JSON, e.g. [[0.1,0.1],[0.9,0.1],[0.5,0.9]].";
        }
        return undefined;
      },
    },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    setSubmitting(true);
    try {
      const zone = await createZone(tenantId, {
        site_id: form.values.site_id,
        name: form.values.name,
        zone_type: form.values.zone_type,
        privacy_level: form.values.privacy_level,
        polygon: JSON.parse(form.values.polygon),
      });
      onCreated(zone);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <Dialog open title="Add a zone" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("site_id")} label="Site" required options={sites.map((s) => ({ value: s.id, label: s.name }))} />
        <Field {...form.field("name")} label="Name" required placeholder="Loading dock" />
        <div className="field-row">
          <Field {...form.field("zone_type")} label="Type" options={ZONE_TYPES.map((t) => ({ value: t, label: t }))} />
          <Field {...form.field("privacy_level")} label="Privacy level" options={PRIVACY_LEVELS.map((p) => ({ value: p, label: p }))} />
        </div>
        <Field
          {...form.field("polygon")}
          label="Polygon"
          required
          multiline
          hint="Normalised [x, y] points, 0 to 1 against the frame. Defaults to nearly the whole frame — edit precisely from the tenant's own camera preview later if needed."
        />
        <FormActions submitting={submitting} submitLabel="Add zone" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
