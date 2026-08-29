"use client";

import { createPipeline } from "../api/pipelines";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, pattern, required, useForm } from "./Form";

const CODE_PATTERN = /^[a-z0-9][a-z0-9_.-]*$/;

/** Creates a new pipeline family - the "template" a version then gets defined under.
 *  Mirrors CreatePipelineRequest in backend/admin_api/app/api/pipelines.py exactly. */
export function CreatePipelineDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const form = useForm({
    code: {
      initial: "",
      label: "Code",
      validate: combine(
        required("Code"),
        maxLength(64, "Code"),
        pattern(CODE_PATTERN, "Code must be lowercase letters, numbers, '.', '_' or '-', starting with a letter or number."),
      ),
    },
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(200, "Name")) },
    use_case: {
      initial: "",
      label: "Use case",
      validate: combine(required("Use case"), maxLength(100, "Use case")),
    },
    description: { initial: "", label: "Description", validate: maxLength(2000, "Description") },
    owner_team: { initial: "", label: "Owner team", validate: maxLength(100, "Owner team") },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const created = await createPipeline({
        code: form.values.code,
        name: form.values.name,
        use_case: form.values.use_case,
        description: form.values.description || undefined,
        owner_team: form.values.owner_team || undefined,
      });
      onCreated(created.id);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title="New pipeline" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field
          {...form.field("code")}
          label="Code"
          required
          placeholder="vehicle-intrusion"
          hint="A stable identifier, never shown to a customer. Cannot be changed after creation."
        />
        <Field {...form.field("name")} label="Name" required placeholder="Vehicle Intrusion Detection" />
        <Field
          {...form.field("use_case")}
          label="Use case"
          required
          placeholder="zone.intrusion"
          hint="Matches the rule type_code this pipeline's detections are meant to drive."
        />
        <Field {...form.field("description")} label="Description" multiline />
        <Field {...form.field("owner_team")} label="Owner team" placeholder="Platform AI" />

        <FormActions submitting={form.submitting} submitLabel="Create pipeline" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
