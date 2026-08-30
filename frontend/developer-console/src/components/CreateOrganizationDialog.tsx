"use client";

import type { CreatedOrganization } from "../api/organizations";
import { createOrganization } from "../api/organizations";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, required, useForm } from "./Form";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Provisions a brand-new organization + tenant with an *invited* owner - mirrors
 *  CreateOrganizationIn in backend/admin_api/app/api/organizations.py exactly. The
 *  platform admin never learns the new owner's password; that person activates through
 *  the invitation email (or the link shown here, if sending failed).
 */
export function CreateOrganizationDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (org: CreatedOrganization) => void;
}) {
  const form = useForm({
    organization_name: {
      initial: "", label: "Organization name",
      validate: combine(required("Organization name"), maxLength(200, "Organization name")),
    },
    organization_type: { initial: "direct_customer", label: "Type" },
    owner_email: {
      initial: "", label: "Owner email",
      validate: (value) => (EMAIL_PATTERN.test(value) ? undefined : "Enter a valid email address."),
    },
    owner_display_name: {
      initial: "", label: "Owner name",
      validate: combine(required("Owner name"), maxLength(200, "Owner name")),
    },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const created = await createOrganization({
        organization_name: form.values.organization_name,
        organization_type: form.values.organization_type as "direct_customer" | "reseller",
        owner_email: form.values.owner_email,
        owner_display_name: form.values.owner_display_name,
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
    <Dialog open title="Create organization" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field {...form.field("organization_name")} label="Organization name" required placeholder="Northwind Security" />
        <Field
          {...form.field("organization_type")}
          label="Type"
          options={[
            { value: "direct_customer", label: "Direct customer" },
            { value: "reseller", label: "Reseller" },
          ]}
          hint="A reseller can create and manage its own child tenants. A reseller_customer tenant is only ever created by a reseller itself, not from here."
        />
        <Field {...form.field("owner_email")} label="Owner email" required type="email" placeholder="owner@northwind.example" />
        <Field {...form.field("owner_display_name")} label="Owner name" required placeholder="Jordan Rivera" />

        <FormActions submitting={form.submitting} submitLabel="Create organization" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
