import { useEffect, useMemo, useRef, useState } from "react";
import { listCameras } from "../api/cameras";
import type { Severity } from "../api/types";
import type { Rule, RuleInput } from "../api/rules";
import { createRule, deleteRule, listRules, updateRule } from "../api/rules";
import { listSites } from "../api/sites";
import type { Zone } from "../api/zones";
import { listZones } from "../api/zones";
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

/** Detection rules — the last step: turning "watch this zone for people" into an alert.
 *
 *  A rule is easy to make silently useless, and the API computes `warnings` from the
 *  values already on the rule rather than the client re-deriving them - a confidence
 *  threshold too high for night, no zone, no cooldown. Shown wherever the rule is seen, so
 *  the moment someone can notice a rule that will never fire is the moment they are
 *  looking at it, not the night an incident goes unraised.
 */

const OBJECT_CLASSES = ["person", "vehicle", "bag", "helmet_missing", "vest_missing"];

const TYPE_CODES = [
  { value: "zone.intrusion", label: "Intrusion — someone entered the zone" },
  { value: "ppe.violation", label: "PPE violation" },
  { value: "fire.smoke", label: "Smoke or fire" },
  { value: "loitering", label: "Loitering" },
  { value: "crowd.density", label: "Crowd density" },
  { value: "vehicle.unauthorised", label: "Unauthorised vehicle" },
  { value: "fall.detected", label: "Fall detected" },
];

const SEVERITIES = [
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

export function RulesPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [siteFilter, setSiteFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Rule | null>(null);
  const [deleting, setDeleting] = useState<Rule | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const rules = useResource(() => listRules({ site_id: siteFilter || undefined }), [siteFilter]);
  const sites = useResource(listSites, []);
  const slow = useSlowRequest(rules.loading);

  const visible = useMemo(() => {
    if (!rules.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return rules.data;
    return rules.data.filter(
      (r) =>
        r.name.toLowerCase().includes(term) ||
        r.type_code.toLowerCase().includes(term) ||
        (r.site_name ?? "").toLowerCase().includes(term) ||
        (r.camera_name ?? "").toLowerCase().includes(term),
    );
  }, [rules.data, search]);

  const hasFilters = search.trim() !== "" || siteFilter !== "";

  async function toggleStatus(rule: Rule) {
    const next = rule.status === "active" ? "disabled" : "active";
    try {
      const saved = await updateRule(rule.id, { status: next });
      rules.mutate((current) => (current ?? []).map((r) => (r.id === rule.id ? saved : r)));
      notify.success(
        next === "disabled" ? `${rule.name} was disabled` : `${rule.name} was re-enabled`,
        next === "disabled" ? "It will not raise any more incidents until re-enabled." : undefined,
      );
    } catch (err) {
      notify.error(
        `Could not update ${rule.name}`,
        err instanceof Error ? err.message : undefined,
      );
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteRule(removed.id);
      rules.mutate((current) => (current ?? []).filter((r) => r.id !== removed.id));
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
          <h1>Rules</h1>
          <p className="muted">
            {rules.data ? `${rules.data.length} rule${rules.data.length === 1 ? "" : "s"}` : " "}
            {rules.refreshing && (
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
          Add rule
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="rule-search">
          Search rules
        </label>
        <input
          id="rule-search"
          type="search"
          placeholder="Search by name, type or site"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="rule-site">
          Filter by site
        </label>
        <select id="rule-site" value={siteFilter} onChange={(e) => setSiteFilter(e.target.value)}>
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

      {slow && rules.loading && <SlowNetworkNotice />}

      {rules.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : Boolean(rules.error) && !rules.data ? (
        <FailureState error={rules.error} online={online} onRetry={rules.reload} entity="rule" />
      ) : visible.length === 0 && hasFilters ? (
        <NoResultsPanel
          query={search.trim() || undefined}
          entity="rules"
          onClear={() => {
            setSearch("");
            setSiteFilter("");
          }}
        />
      ) : (sites.data?.length ?? 0) === 0 ? (
        <EmptyPanel title="Add a site first" icon="⌂">
          <p>A rule is written against a site's cameras and zones, so a site has to exist first.</p>
        </EmptyPanel>
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No rules yet"
          icon="⚑"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Create your first rule
            </button>
          }
        >
          <p>
            A rule is what turns a detection into an incident — "tell me when a person
            enters this zone". Without one, cameras record but nothing alerts.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Detection rules</caption>
            <thead>
              <tr>
                <th scope="col">Rule</th>
                <th scope="col">Watching</th>
                <th scope="col">Threshold</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((rule) => (
                <tr key={rule.id}>
                  <td>
                    <strong>{rule.name}</strong>
                    <div className="muted">
                      {TYPE_CODES.find((t) => t.value === rule.type_code)?.label.split(" — ")[0] ??
                        rule.type_code}
                    </div>
                    <SeverityBadge severity={rule.severity as Severity} />
                  </td>
                  <td>
                    {rule.camera_name ?? <span className="muted">Every camera</span>}
                    <div className="muted">{rule.zone_name ?? "Whole frame"}</div>
                    <div className="muted">{rule.alertable_classes.join(", ")}</div>
                  </td>
                  <td>
                    {(rule.min_confidence * 100).toFixed(0)}% confidence
                    <div className="muted">
                      {rule.active_from_hour === null
                        ? "Always active"
                        : `${String(rule.active_from_hour).padStart(2, "0")}:00–${String(
                            rule.active_to_hour,
                          ).padStart(2, "0")}:00 ${rule.site_timezone ?? ""}`}
                    </div>
                  </td>
                  <td>
                    <span className={`pill pill-${rule.status === "active" ? "ready" : "disabled"}`}>
                      {rule.status}
                    </span>
                    {rule.warnings.length > 0 && (
                      // A one-line count in the table; the reasons are in the edit dialog
                      // and the create form, where there is room to explain them properly.
                      <div className="muted rule-warning-flag" title={rule.warnings.join(" ")}>
                        ⚠ {rule.warnings.length} warning{rule.warnings.length === 1 ? "" : "s"}
                      </div>
                    )}
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => void toggleStatus(rule)}
                      disabled={!online}
                    >
                      {rule.status === "active" ? "Disable" : "Enable"}
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(rule)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(rule)}
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

      {Boolean(rules.error) && rules.data && (
        <div className="notice notice-warning" role="alert" style={{ marginTop: 16 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>This list may be out of date.</strong>
            <p>The last refresh failed. What you see was loaded earlier.</p>
          </div>
          <button type="button" className="btn-quiet" onClick={rules.reload}>
            Retry
          </button>
        </div>
      )}

      {(creating || editing) && (
        <RuleFormDialog
          rule={editing}
          sites={sites.data ?? []}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(rule, wasNew) => {
            rules.mutate((current) => {
              const list = current ?? [];
              return wasNew ? [...list, rule] : list.map((r) => (r.id === rule.id ? rule : r));
            });
            notify.success(wasNew ? `${rule.name} was created` : `${rule.name} was updated`);
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this rule?"
        busy={deleteBusy}
        confirmLabel="Remove rule"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> will stop watching for anything. This cannot
              be undone — disable it instead if you might want it back.
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

function RuleFormDialog({
  rule,
  sites,
  onClose,
  onSaved,
}: {
  rule: Rule | null;
  sites: { id: string; name: string }[];
  onClose: () => void;
  onSaved: (rule: Rule, wasNew: boolean) => void;
}) {
  const isNew = rule === null;
  const [selectedClasses, setSelectedClasses] = useState<string[]>(
    rule?.alertable_classes ?? ["person"],
  );
  const [useHours, setUseHours] = useState(rule?.active_from_hour !== null && rule !== null);

  const form = useForm({
    site_id: { initial: rule?.site_id ?? sites[0]?.id ?? "", label: "Site", validate: required("Site") },
    camera_id: { initial: rule?.camera_id ?? "", label: "Camera" },
    zone_id: { initial: rule?.zone_id ?? "", label: "Zone" },
    name: {
      initial: rule?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(120, "Name")),
    },
    type_code: { initial: rule?.type_code ?? "zone.intrusion", label: "Type" },
    severity: { initial: rule?.severity ?? "high", label: "Severity" },
    min_confidence: {
      initial: String(rule?.min_confidence ?? 0.4),
      label: "Confidence threshold",
      validate: (value) => {
        const n = Number(value);
        return Number.isFinite(n) && n > 0 && n <= 1
          ? undefined
          : "Enter a number greater than 0 and no more than 1.";
      },
    },
    cooldown_seconds: {
      initial: String(rule?.cooldown_seconds ?? 300),
      label: "Cooldown (seconds)",
      validate: (value) => {
        const n = Number(value);
        return Number.isInteger(n) && n >= 0 ? undefined : "Enter a whole number, 0 or more.";
      },
    },
    from_hour: { initial: rule?.active_from_hour?.toString() ?? "22", label: "Active from" },
    to_hour: { initial: rule?.active_to_hour?.toString() ?? "6", label: "Active until" },
  });

  // Cameras and zones are scoped to the chosen site, so choosing a site first is what
  // makes the rest of the form make sense - and re-fetched whenever it changes.
  const camerasForSite = useResource(
    () => (form.values.site_id ? listCameras({ site_id: form.values.site_id }) : Promise.resolve([])),
    [form.values.site_id],
  );
  const zonesForSite = useResource(
    () => (form.values.site_id ? listZones({ site_id: form.values.site_id }) : Promise.resolve([] as Zone[])),
    [form.values.site_id],
  );

  // If the site changes, a camera or zone chosen under the old one no longer applies.
  const previousSite = useRef(form.values.site_id);
  useEffect(() => {
    if (previousSite.current !== form.values.site_id) {
      form.setValue("camera_id", "");
      form.setValue("zone_id", "");
      previousSite.current = form.values.site_id;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.values.site_id]);

  const confidenceValue = Number(form.values.min_confidence);
  const nightWarning =
    Number.isFinite(confidenceValue) && confidenceValue > 0.45
      ? "At this threshold, detections on a dark or infrared frame are likely to be missed — a person in darkness typically scores 0.1–0.2."
      : undefined;

  function toggleClass(cls: string) {
    setSelectedClasses((current) =>
      current.includes(cls) ? current.filter((c) => c !== cls) : [...current, cls],
    );
  }

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    if (selectedClasses.length === 0) {
      form.setFormError("Choose at least one object class.");
      return;
    }
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const body: RuleInput = {
        site_id: form.values.site_id,
        camera_id: form.values.camera_id || null,
        zone_id: form.values.zone_id || null,
        name: form.values.name,
        type_code: form.values.type_code,
        alertable_classes: selectedClasses,
        min_confidence: Number(form.values.min_confidence),
        severity: form.values.severity,
        cooldown_seconds: Number(form.values.cooldown_seconds),
        active_from_hour: useHours ? Number(form.values.from_hour) : null,
        active_to_hour: useHours ? Number(form.values.to_hour) : null,
      };
      const saved = isNew ? await createRule(body) : await updateRule(rule.id, body);
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  const cameraOptions = [
    { value: "", label: "Every camera at this site" },
    ...(camerasForSite.data ?? []).map((c) => ({ value: c.id, label: c.name })),
  ];
  const zoneOptions = [
    { value: "", label: "Whole frame" },
    ...(zonesForSite.data ?? []).map((z) => ({ value: z.id, label: z.name })),
  ];
  const siteOptions = sites.map((s) => ({ value: s.id, label: s.name }));
  const hourOptions = Array.from({ length: 24 }, (_, h) => ({
    value: String(h),
    label: `${String(h).padStart(2, "0")}:00`,
  }));

  return (
    <Dialog open title={isNew ? "Create a rule" : `Edit ${rule.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        {!isNew && rule.warnings.length > 0 && (
          <div className="notice notice-warning" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>This rule currently has {rule.warnings.length} issue{rule.warnings.length === 1 ? "" : "s"}.</strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {rule.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <Field {...form.field("name")} label="Name" required placeholder="No entry to the dock" />
        <Field {...form.field("site_id")} label="Site" required options={siteOptions} />

        <div className="field-row">
          <Field
            {...form.field("camera_id")}
            label="Camera"
            options={cameraOptions}
            hint="Leave as 'every camera' to apply this rule site-wide."
          />
          <Field
            {...form.field("zone_id")}
            label="Zone"
            options={zoneOptions}
            hint="Leave as 'whole frame' to watch everything the camera sees."
          />
        </div>

        <Field {...form.field("type_code")} label="What to watch for" options={TYPE_CODES} />

        <div className="field">
          <label>
            Object classes<span className="field-required" aria-hidden="true"> *</span>
          </label>
          <p className="field-hint">Which detected objects can trigger this rule.</p>
          <div className="chip-row">
            {OBJECT_CLASSES.map((cls) => (
              <label key={cls} className="chip">
                <input
                  type="checkbox"
                  checked={selectedClasses.includes(cls)}
                  onChange={() => toggleClass(cls)}
                />
                {cls.replace("_", " ")}
              </label>
            ))}
          </div>
        </div>

        <Field {...form.field("severity")} label="Severity" options={SEVERITIES} />

        <div className="field-row">
          <Field
            {...form.field("min_confidence")}
            label="Confidence threshold"
            hint={nightWarning ?? "0 to 1. Higher means fewer false alarms, but a higher chance of missing a real one."}
          />
          <Field
            {...form.field("cooldown_seconds")}
            label="Cooldown (seconds)"
            hint="How long before the same situation can raise another incident."
          />
        </div>
        {nightWarning && (
          <div className="notice notice-warning" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>This threshold is likely too high for night footage.</strong>
              <p>{nightWarning}</p>
            </div>
          </div>
        )}

        <div className="field">
          <label>
            <input
              type="checkbox"
              checked={useHours}
              onChange={(e) => setUseHours(e.target.checked)}
              style={{ marginRight: 8 }}
            />
            Only active during specific hours
          </label>
          {useHours && (
            <div className="field-row" style={{ marginTop: 8 }}>
              <Field {...form.field("from_hour")} label="From" options={hourOptions} />
              <Field {...form.field("to_hour")} label="Until" options={hourOptions} />
            </div>
          )}
          <p className="field-hint">
            Evaluated in the site's own timezone
            {sites.find((s) => s.id === form.values.site_id) ? "" : " — choose a site to see it"}
            .
          </p>
        </div>

        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Create rule" : "Save changes"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}
