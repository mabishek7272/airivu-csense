"use client";

import type { PipelineVersionOut, PipelineVersionRow } from "../api/pipelines";
import { deprecatePipelineVersion, publishPipelineVersion } from "../api/pipelines";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, minLength, onSubmitHandler, required, useForm } from "./Form";

/** Publish (draft -> published) or deprecate (published -> deprecated) a pipeline
 *  version - the two transitions VERSION_TRANSITIONS in pipelines.py actually allows, so
 *  unlike PromoteDialog's dropdown there's exactly one target per state and this just
 *  asks for the reason that goes in the audit trail.
 *
 *  Takes a `PipelineVersionRow`, not the wider `PipelineVersionOut` - this only ever
 *  opens from a row in a pipeline's version table, which by construction always has a
 *  real version (see groupByPipeline). */
export function PipelineTransitionDialog({
  version,
  action,
  onClose,
  onTransitioned,
}: {
  version: PipelineVersionRow;
  action: "publish" | "deprecate";
  onClose: () => void;
  onTransitioned: (updated: PipelineVersionOut) => void;
}) {
  const form = useForm({
    reason: {
      initial: "",
      label: "Reason",
      validate: combine(required("Reason"), minLength(8, "Reason"), maxLength(500, "Reason")),
    },
  });

  const label = action === "publish" ? "Publish" : "Deprecate";
  const call = action === "publish" ? publishPipelineVersion : deprecatePipelineVersion;

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const updated = await call(version.version_id, { reason: form.values.reason });
      onTransitioned(updated);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={`${label} ${version.code} v${version.version_number}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <p>
          Currently: <strong>{version.state}</strong>
        </p>
        {action === "publish" && (
          <p className="field-hint">
            Publishing makes this version immutable and assignable to a tenant&apos;s camera. It cannot be
            un-published - only deprecated, or superseded by a new version.
          </p>
        )}
        {action === "deprecate" && (
          <p className="field-hint">
            Existing camera assignments pointed at this version are unaffected; no new assignment can be
            created against it once deprecated.
          </p>
        )}

        <Field
          {...form.field("reason")}
          label="Reason"
          required
          multiline
          placeholder={`Why is this version being ${action === "publish" ? "published" : "deprecated"}?`}
          hint="Recorded in the audit trail."
        />

        <FormActions submitting={form.submitting} submitLabel={label} onCancel={onClose} />
      </form>
    </Dialog>
  );
}
