"use client";

import { useState } from "react";
import type { ModelVersionOut } from "../api/modelRegistry";
import { DEPLOYABLE_STATES, VALID_TRANSITIONS, promoteModelVersion } from "../api/modelRegistry";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, minLength, onSubmitHandler, required, useForm } from "./Form";

/** Moves a model version through its lifecycle state.
 *
 *  The target-state dropdown only ever offers states the server's own
 *  VALID_TRANSITIONS table (backend/admin_api/app/api/models.py) allows from here - an
 *  operator cannot even attempt an illegal hop from this form. That mirror can still go
 *  stale (another operator promotes the same version between page-load and submit); the
 *  resulting 409 is caught and shown like any other server error, never assumed away.
 *
 *  The biometric-acknowledgement notice appears only when it would actually matter -
 *  same gate the server itself applies - and leaving it unchecked still lets the request
 *  fire, so the 422 the server sends back is what actually proves the gate is real, not
 *  just a client-side guess at it.
 */
export function PromoteDialog({
  version,
  onClose,
  onPromoted,
}: {
  version: ModelVersionOut;
  onClose: () => void;
  onPromoted: (updated: ModelVersionOut) => void;
}) {
  const targets = VALID_TRANSITIONS[version.state] ?? [];
  const [acknowledged, setAcknowledged] = useState(false);

  const form = useForm({
    target_state: { initial: targets[0] ?? "", label: "Target state" },
    reason: {
      initial: "",
      label: "Reason",
      validate: combine(required("Reason"), minLength(8, "Reason"), maxLength(500, "Reason")),
    },
  });

  const isBiometric = version.access_classification === "biometric";
  const targetIsDeployable = DEPLOYABLE_STATES.has(form.values.target_state);
  const needsAcknowledgement = isBiometric && targetIsDeployable;

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const updated = await promoteModelVersion(version.id, {
        target_state: form.values.target_state,
        reason: form.values.reason,
        acknowledge_biometric: needsAcknowledgement ? acknowledged : undefined,
      });
      onPromoted(updated);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  if (targets.length === 0) {
    // `revoked` -> `validating` is always available, so this is only reachable if a
    // state somehow has no entry at all - defensive, not expected in practice.
    return (
      <Dialog
        open
        title={`Promote ${version.model_name} ${version.version_label}`}
        onClose={onClose}
        footer={<button type="button" onClick={onClose}>Close</button>}
      >
        <p>
          This version is in state <strong>{version.state}</strong>, which has no further
          transition defined.
        </p>
      </Dialog>
    );
  }

  return (
    <Dialog
      open
      title={`Promote ${version.model_name} ${version.version_label}`}
      onClose={onClose}
    >
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <p>
          Currently: <strong>{version.state}</strong>
        </p>

        <Field
          {...form.field("target_state")}
          label="Target state"
          options={targets.map((t) => ({ value: t, label: t }))}
        />

        <Field
          {...form.field("reason")}
          label="Reason"
          required
          multiline
          placeholder="Why is this version moving state?"
          hint={`Recorded in the audit trail. Explain why this version is moving to ${form.values.target_state || "the target state"}.`}
        />

        {needsAcknowledgement && (
          <div className="notice notice-warning" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>This is a biometric model.</strong>
              <p>
                Facial recognition is outside the approved release-one scope, and
                biometric templates carry additional legal obligations. Confirming below
                records in the audit trail that privacy and legal review is complete for
                this promotion.
              </p>
              <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 8 }}>
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                />
                <span>I confirm privacy and legal review for this biometric model is complete.</span>
              </label>
            </div>
          </div>
        )}

        <FormActions submitting={form.submitting} submitLabel="Promote" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
