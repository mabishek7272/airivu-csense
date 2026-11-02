"use client";

import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import type { Camera, Rule, Site, Zone } from "@/api/tenantInfra";
import { createRule, deleteRule, listCameras, listRules, listSites, listZones, updateRule } from "@/api/tenantInfra";
import { ConfirmDialog, Dialog } from "@/components/Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, required, useForm } from "@/components/Form";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

const TYPE_CODES = ["zone.intrusion", "ppe.violation", "fire.smoke", "loitering", "crowd.density", "vehicle.unauthorised", "fall.detected", "face.unknown", "face.watchlist"];
const SEVERITIES = ["low", "medium", "high", "critical"];
// "fire" and "smoke" confirmed against real detection rows the fire-smoke-detection
// pipeline has actually written (`select distinct objects->>'class' from detections`),
// not guessed from the model's name - the other five were already in use by rule-based
// (not model-based) checks, e.g. "helmet_missing"/"vest_missing" are PPE-rule outputs.
const OBJECT_CLASSES = ["person", "vehicle", "bag", "helmet_missing", "vest_missing", "fire", "smoke", "face:unknown", "face:watchlist"];

export default function TenantRulesPage({ params }: { params: { tenantId: string } }) {
  const { tenantId } = params;
  const { isAuthenticated } = useAuth();
  const online = useOnlineStatus();
  const notify = useNotifications();

  const rules = useResource(() => listRules(tenantId), [isAuthenticated, tenantId]);
  const sites = useResource(() => listSites(tenantId), [isAuthenticated, tenantId]);
  const cameras = useResource(() => listCameras(tenantId), [isAuthenticated, tenantId]);
  const zones = useResource(() => listZones(tenantId), [isAuthenticated, tenantId]);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Rule | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function toggleStatus(rule: Rule) {
    const next = rule.status === "active" ? "disabled" : "active";
    try {
      const saved = await updateRule(tenantId, rule.id, { status: next });
      rules.mutate((current) => (current ?? []).map((r) => (r.id === rule.id ? saved : r)));
    } catch (err) {
      notify.error(`Could not update ${rule.name}`, err instanceof Error ? err.message : undefined);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await deleteRule(tenantId, deleting.id);
      rules.mutate((current) => (current ?? []).filter((r) => r.id !== deleting.id));
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
          <h2>Rules</h2>
          <p>{rules.data ? `${rules.data.length} rule${rules.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online || (sites.data ?? []).length === 0} onClick={() => setCreating(true)}>
          Create rule
        </button>
      </div>

      {rules.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : rules.error && !rules.data ? (
        <FailureState error={rules.error} online={online} onRetry={rules.reload} entity="rules" />
      ) : rules.data && rules.data.length === 0 ? (
        <EmptyPanel title="No rules yet" icon="○">
          <p>A rule turns a detection into an incident. Nothing is raised until one exists.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Rules</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Confidence</th>
                <th scope="col">Severity</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(rules.data ?? []).map((rule) => (
                <tr key={rule.id}>
                  <td>
                    <strong>{rule.name}</strong>
                    <div className="muted">{rule.camera_name ?? "Every camera"} · {rule.zone_name ?? "Whole frame"}</div>
                    {rule.warnings.length > 0 && <div className="muted">⚠ {rule.warnings[0]}</div>}
                  </td>
                  <td className="mono">{rule.type_code}</td>
                  <td>{rule.min_confidence.toFixed(2)}</td>
                  <td>
                    <span className="pill">{rule.severity}</span>
                  </td>
                  <td>
                    <span className={`pill pill-${rule.status === "active" ? "online" : "disabled"}`}>{rule.status}</span>
                  </td>
                  <td className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => toggleStatus(rule)} disabled={!online}>
                      {rule.status === "active" ? "Disable" : "Enable"}
                    </button>
                    <button type="button" className="btn-quiet" onClick={() => setDeleting(rule)} disabled={!online}>
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
        <CreateRuleDialog
          tenantId={tenantId}
          sites={sites.data ?? []}
          cameras={cameras.data ?? []}
          zones={zones.data ?? []}
          onClose={() => setCreating(false)}
          onCreated={(rule) => {
            rules.mutate((current) => [...(current ?? []), rule]);
            notify.success(`${rule.name} created`);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={`Remove ${deleting?.name ?? ""}`}
        body={<p>This cannot be undone. Disable it instead to keep it around but inactive.</p>}
        confirmLabel="Remove"
        busy={deleteBusy}
        onConfirm={handleDelete}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}

function CreateRuleDialog({
  tenantId,
  sites,
  cameras,
  zones,
  onClose,
  onCreated,
}: {
  tenantId: string;
  sites: Site[];
  cameras: Camera[];
  zones: Zone[];
  onClose: () => void;
  onCreated: (rule: Rule) => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [selectedClasses, setSelectedClasses] = useState<string[]>(["person"]);

  const form = useForm({
    site_id: { initial: sites[0]?.id ?? "", label: "Site", validate: required("Site") },
    camera_id: { initial: "", label: "Camera" },
    zone_id: { initial: "", label: "Zone" },
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(120, "Name")) },
    type_code: { initial: "zone.intrusion", label: "What to watch for" },
    severity: { initial: "high", label: "Severity" },
    min_confidence: { initial: "0.4", label: "Confidence threshold" },
    cooldown_seconds: { initial: "300", label: "Cooldown (seconds)" },
  });

  function toggleClass(cls: string) {
    setSelectedClasses((current) => (current.includes(cls) ? current.filter((c) => c !== cls) : [...current, cls]));
  }

  const confidence = Number(form.values.min_confidence);
  const nightWarning =
    confidence > 0.45
      ? `At ${confidence.toFixed(2)}, this will likely miss detections at night (an infrared frame scores a person around 0.1–0.2).`
      : null;

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    if (selectedClasses.length === 0) {
      form.setFormError("Choose at least one object class.");
      return;
    }
    setSubmitting(true);
    try {
      const rule = await createRule(tenantId, {
        site_id: form.values.site_id,
        camera_id: form.values.camera_id || undefined,
        zone_id: form.values.zone_id || undefined,
        name: form.values.name,
        type_code: form.values.type_code,
        alertable_classes: selectedClasses,
        min_confidence: confidence,
        severity: form.values.severity,
        cooldown_seconds: Number(form.values.cooldown_seconds),
      });
      onCreated(rule);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      setSubmitting(false);
    }
  });

  const siteCameras = cameras.filter((c) => c.site_id === form.values.site_id);
  const siteZones = zones.filter((z) => z.site_id === form.values.site_id);

  return (
    <Dialog open title="Create a rule" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("name")} label="Name" required placeholder="No entry to the dock" />
        <Field {...form.field("site_id")} label="Site" required options={sites.map((s) => ({ value: s.id, label: s.name }))} />
        <div className="field-row">
          <Field
            {...form.field("camera_id")}
            label="Camera"
            options={[{ value: "", label: "Every camera" }, ...siteCameras.map((c) => ({ value: c.id, label: c.name }))]}
            hint="Leave as 'every camera' to apply this rule site-wide."
          />
          <Field
            {...form.field("zone_id")}
            label="Zone"
            options={[{ value: "", label: "Whole frame" }, ...siteZones.map((z) => ({ value: z.id, label: z.name }))]}
            hint="Leave as 'whole frame' to watch everything the camera sees."
          />
        </div>
        <Field {...form.field("type_code")} label="What to watch for" options={TYPE_CODES.map((t) => ({ value: t, label: t }))} />
        <div className="field">
          <label>
            Object classes<span className="field-required" aria-hidden="true"> *</span>
          </label>
          <div className="chip-row">
            {OBJECT_CLASSES.map((cls) => (
              <label key={cls} className="chip">
                <input type="checkbox" checked={selectedClasses.includes(cls)} onChange={() => toggleClass(cls)} />
                {cls.replace("_", " ")}
              </label>
            ))}
          </div>
        </div>
        <Field {...form.field("severity")} label="Severity" options={SEVERITIES.map((s) => ({ value: s, label: s }))} />
        <div className="field-row">
          <Field {...form.field("min_confidence")} label="Confidence threshold" hint={nightWarning ?? "0 to 1. Higher means fewer false alarms, but a higher chance of missing a real one."} />
          <Field {...form.field("cooldown_seconds")} label="Cooldown (seconds)" />
        </div>
        {nightWarning && (
          <div className="notice notice-warning" style={{ marginBottom: 16 }}>
            <strong>This threshold is likely too high for night footage.</strong>
            <p>{nightWarning}</p>
          </div>
        )}
        <FormActions submitting={submitting} submitLabel="Create rule" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
