"use client";

import type { License, LicensePlan } from "../api/licensing";
import { issueLicense } from "../api/licensing";
import type { Organization } from "../api/organizations";
import { verifyMfa } from "../api/mfa";
import { ApiRequestError } from "../api/client";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, required, onSubmitHandler, useForm } from "./Form";

/** Issues a license to a tenant - mirrors IssueLicenseIn in
 *  backend/admin_api/app/api/licensing.py. Issuance requires a *recent* step-up
 *  verification (TRD-SEC-010), not just the session's own bearer token - rather than
 *  guessing whether one is still fresh, this dialog always asks for a current code and
 *  verifies right before issuing, in the same submit. If the account has no MFA enrolled
 *  at all, verification fails with a clear message pointing at Settings, not a confusing
 *  step-up-specific error.
 */
export function IssueLicenseDialog({
  organizations,
  plans,
  onClose,
  onIssued,
}: {
  organizations: Organization[];
  plans: LicensePlan[];
  onClose: () => void;
  onIssued: (license: License) => void;
}) {
  const orgsWithTenants = organizations.filter((o) => o.tenant_id);

  const form = useForm({
    tenant_id: { initial: orgsWithTenants[0]?.tenant_id ?? "", label: "Tenant", validate: required("Tenant") },
    plan_code: { initial: plans[0]?.code ?? "", label: "Plan", validate: required("Plan") },
    code: { initial: "", label: "Verification code", validate: required("Verification code") },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      await verifyMfa({ code: form.values.code });
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        form.setFormError(
          err.body.message.includes("not enrolled")
            ? "Two-factor authentication isn't enabled for your account yet - turn it on in Settings before issuing a license."
            : "That code is incorrect. Check your authenticator app and try again.",
        );
      } else {
        form.applyServerError(err);
      }
      form.setSubmitting(false);
      return;
    }

    try {
      const license = await issueLicense({ tenant_id: form.values.tenant_id, plan_code: form.values.plan_code });
      onIssued(license);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title="Issue license" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        {orgsWithTenants.length > 0 ? (
          <Field
            {...form.field("tenant_id")}
            label="Tenant"
            required
            options={orgsWithTenants.map((o) => ({ value: o.tenant_id as string, label: `${o.display_name} (${o.organization_type})` }))}
          />
        ) : (
          <Field {...form.field("tenant_id")} label="Tenant id" required placeholder="00000000-0000-0000-0000-000000000000" />
        )}

        {plans.length > 0 ? (
          <Field
            {...form.field("plan_code")}
            label="Plan"
            required
            options={plans.map((p) => ({ value: p.code, label: `${p.name} (${p.code})` }))}
          />
        ) : (
          <Field {...form.field("plan_code")} label="Plan code" required placeholder="starter-annual" />
        )}

        <Field
          {...form.field("code")}
          label="Verification code"
          required
          type="text"
          placeholder="6-digit code"
          hint="A recent MFA verification is required to issue a license (TRD-SEC-010)."
        />

        <FormActions submitting={form.submitting} submitLabel="Issue license" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
