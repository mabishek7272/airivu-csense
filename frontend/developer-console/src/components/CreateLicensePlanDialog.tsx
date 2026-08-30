"use client";

import { useState } from "react";
import type { LicensePlan } from "../api/licensing";
import { createLicensePlan } from "../api/licensing";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, pattern, required, useForm } from "./Form";

const CODE_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

interface EntitlementRow {
  code: string;
  limit: string;
}

/** Creates a license plan - mirrors CreateLicensePlanIn in
 *  backend/admin_api/app/api/licensing.py exactly.
 *
 *  **Numeric limits only, stated plainly**: `license_entitlements.value_type` also
 *  supports `boolean` and `json` (a feature flag, an arbitrary structured limit), but
 *  every entitlement this deployment actually uses (`camera.count`) is a numeric limit -
 *  the other two types are real schema with no UI yet, not a technical ceiling.
 */
export function CreateLicensePlanDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (plan: LicensePlan) => void;
}) {
  const [entitlements, setEntitlements] = useState<EntitlementRow[]>([{ code: "camera.count", limit: "" }]);
  const [error, setError] = useState<string | undefined>();

  const form = useForm({
    code: {
      initial: "", label: "Code",
      validate: combine(
        required("Code"), maxLength(64, "Code"),
        pattern(CODE_PATTERN, "Code must be lowercase letters, numbers, '_' or '-', starting with a letter or number."),
      ),
    },
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(200, "Name")) },
    license_type: { initial: "standard", label: "License type", validate: required("License type") },
    billing_period: { initial: "yearly", label: "Billing period" },
  });

  function updateRow(index: number, patch: Partial<EntitlementRow>) {
    setEntitlements((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    setError(undefined);
    form.setFormError(undefined);

    const default_entitlements: Record<string, { value_type: "limit_numeric"; limit_numeric: number }> = {};
    for (const row of entitlements) {
      if (!row.code.trim() || !row.limit.trim()) continue;
      const numeric = Number(row.limit);
      if (!Number.isFinite(numeric) || numeric < 0) {
        setError(`"${row.code}" needs a whole, non-negative number.`);
        form.setSubmitting(false);
        return;
      }
      default_entitlements[row.code.trim()] = { value_type: "limit_numeric", limit_numeric: numeric };
    }

    try {
      const created = await createLicensePlan({
        code: form.values.code,
        name: form.values.name,
        license_type: form.values.license_type,
        billing_period: form.values.billing_period as "quarterly" | "half_yearly" | "yearly",
        default_entitlements,
      });
      onCreated(created);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title="Create license plan" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field {...form.field("code")} label="Code" required placeholder="starter-annual" hint="Cannot be changed after creation." />
        <Field {...form.field("name")} label="Name" required placeholder="Starter (annual)" />
        <Field {...form.field("license_type")} label="License type" required placeholder="standard" />
        <Field
          {...form.field("billing_period")}
          label="Billing period"
          options={[
            { value: "quarterly", label: "Quarterly" },
            { value: "half_yearly", label: "Half-yearly" },
            { value: "yearly", label: "Yearly" },
          ]}
        />

        <div className="field">
          <label>Default entitlements</label>
          <p className="field-hint">
            Numeric limits only (e.g. <code>camera.count</code>). Leave a limit blank to skip that row.
          </p>
          {entitlements.map((row, i) => (
            <div key={i} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
              <input
                placeholder="entitlement code"
                value={row.code}
                onChange={(e) => updateRow(i, { code: e.target.value })}
                style={{ flex: 2 }}
              />
              <input
                placeholder="limit"
                inputMode="numeric"
                value={row.limit}
                onChange={(e) => updateRow(i, { limit: e.target.value.replace(/[^\d]/g, "") })}
                style={{ flex: 1 }}
              />
              <button
                type="button"
                className="btn-quiet"
                onClick={() => setEntitlements((rows) => rows.filter((_, idx) => idx !== i))}
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn-quiet"
            onClick={() => setEntitlements((rows) => [...rows, { code: "", limit: "" }])}
          >
            Add entitlement
          </button>
          {error && (
            <p className="field-error" role="alert">
              <span aria-hidden="true">✕ </span>
              {error}
            </p>
          )}
        </div>

        <FormActions submitting={form.submitting} submitLabel="Create plan" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
