"use client";

import type { License, LicensePlan } from "../api/licensing";
import { changeLicensePlan } from "../api/licensing";
import { verifyMfa } from "../api/mfa";
import { ApiRequestError } from "../api/client";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, required, onSubmitHandler, useForm } from "./Form";

/** Changes an existing license's plan - mirrors ChangeLicensePlanIn in
 *  backend/admin_api/app/api/licensing.py. Same step-up-then-submit shape as
 *  IssueLicenseDialog (TRD-SEC-010): the code is verified right before the change, in
 *  the same submit, rather than trusting the session's own bearer token alone.
 */
export function ChangeLicensePlanDialog({
  license,
  plans,
  onClose,
  onChanged,
}: {
  license: License;
  plans: LicensePlan[];
  onClose: () => void;
  onChanged: (license: License) => void;
}) {
  const otherPlans = plans.filter((p) => p.code !== license.plan_code);

  const form = useForm({
    new_plan_code: { initial: otherPlans[0]?.code ?? "", label: "New plan", validate: required("New plan") },
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
            ? "Two-factor authentication isn't enabled for your account yet - turn it on in Settings before changing a plan."
            : "That code is incorrect. Check your authenticator app and try again.",
        );
      } else {
        form.applyServerError(err);
      }
      form.setSubmitting(false);
      return;
    }

    try {
      const updated = await changeLicensePlan(license.id, { new_plan_code: form.values.new_plan_code });
      onChanged(updated);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={`Change plan (currently ${license.plan_code})`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        {otherPlans.length > 0 ? (
          <Field
            {...form.field("new_plan_code")}
            label="New plan"
            required
            options={otherPlans.map((p) => ({ value: p.code, label: `${p.name} (${p.code})` }))}
          />
        ) : (
          <p className="muted">No other active plan exists to switch to.</p>
        )}

        <Field
          {...form.field("code")}
          label="Verification code"
          required
          type="text"
          placeholder="6-digit code"
          hint="A recent MFA verification is required to change a plan (TRD-SEC-010)."
        />

        <FormActions
          submitting={form.submitting}
          submitLabel="Change plan"
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}
