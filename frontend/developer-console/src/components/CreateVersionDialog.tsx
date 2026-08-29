"use client";

import { useMemo } from "react";
import { DEPLOYABLE_STATES, listModelVersions } from "../api/modelRegistry";
import { createPipelineVersion } from "../api/pipelines";
import type { PipelineVersionOut } from "../api/pipelines";
import { useResource } from "../hooks/useResource";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, onSubmitHandler, required, useForm } from "./Form";

/** Creates a new draft version under an existing pipeline.
 *
 *  A model dropdown, not a stage editor: `infer` is the only stage type any runtime
 *  interprets today (see pipelines.py's own docstring), so this asks for exactly the one
 *  thing that means anything yet - which model - rather than building a general-purpose
 *  pipeline builder for stage types nothing executes. The dropdown is filtered to models
 *  with at least one version in a deployable state, mirroring DEPLOYABLE_STATES - the
 *  server re-validates this regardless, so a stale mirror here can only ever offer a
 *  model the server then rejects, never bypass the real check.
 */
export function CreateVersionDialog({
  pipelineId,
  pipelineName,
  onClose,
  onCreated,
}: {
  pipelineId: string;
  pipelineName: string;
  onClose: () => void;
  onCreated: (version: PipelineVersionOut) => void;
}) {
  const models = useResource(() => listModelVersions(), []);

  const deployableModelNames = useMemo(() => {
    const names = new Set<string>();
    for (const v of models.data ?? []) {
      if (DEPLOYABLE_STATES.has(v.state)) names.add(v.model_name);
    }
    return Array.from(names).sort();
  }, [models.data]);

  const form = useForm({
    model_name: { initial: "", label: "Model", validate: required("Model") },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const version = await createPipelineVersion(pipelineId, {
        stages: [{ type: "infer", model_name: form.values.model_name, min_model_state: "validated" }],
      });
      onCreated(version);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={`New version — ${pipelineName}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        {models.loading ? (
          <p>Loading models…</p>
        ) : deployableModelNames.length === 0 ? (
          <p className="notice notice-warning">
            No model has a version in a deployable state ({Array.from(DEPLOYABLE_STATES).sort().join(", ")}) yet -
            promote one on the Models page first.
          </p>
        ) : (
          <Field
            {...form.field("model_name")}
            label="Model"
            required
            options={[
              { value: "", label: "Select a model…" },
              ...deployableModelNames.map((name) => ({ value: name, label: name })),
            ]}
            hint="The version's inference stage runs this model. A new pipeline version is created in draft state."
          />
        )}

        <FormActions
          submitting={form.submitting}
          submitLabel="Create version"
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}
