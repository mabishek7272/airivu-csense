"use client";

import { useCallback, useId, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ApiRequestError } from "../api/client";

/** Form fields, validation and submission.
 *
 *  Port of frontend/customer-crm/src/components/Form.tsx, with two additions this
 *  console needs that the CRM's original doesn't: a `minLength` validator (the promote
 *  dialog's reason field has a floor as well as a ceiling) and a `textarea` variant on
 *  `Field` (a reason is prose, not a single line). Everything else is unchanged - kept in
 *  this app's own copy rather than importing the CRM's, since the two apps are
 *  deliberately not shared code (see api/client.ts's own comment on why).
 *
 *  **When an error appears matters more than how it looks.** Validating on every keystroke
 *  means telling someone their input is invalid while they are still on the third
 *  character - the message is correct, useless, and hostile. So a field is only marked
 *  invalid after it has been blurred, or after a submit attempt. Once a field *is*
 *  showing an error, it re-validates as you type, because at that point you are actively
 *  fixing it and want to see it clear.
 *
 *  **Server-side validation errors land on the fields they belong to.** FastAPI reports
 *  which field failed; dropping that into a generic banner makes the user hunt for it.
 *
 *  **The error summary exists for screen readers and keyboard users.** Scrolling to the
 *  first bad field is easy with a mouse and miserable without; the summary is focusable
 *  and links to each field.
 */

export type Validator<T> = (value: T, all: Record<string, unknown>) => string | undefined;

export interface FieldConfig<T = string> {
  initial: T;
  validate?: Validator<T>;
  label: string;
}

export interface FieldState {
  value: string;
  error?: string;
  touched: boolean;
}

/* -------------------------------------------------------------- validators */

export const required =
  (label: string): Validator<string> =>
  (value) =>
    value.trim() ? undefined : `${label} is required.`;

export const minLength =
  (limit: number, label: string): Validator<string> =>
  (value) =>
    !value || value.trim().length >= limit
      ? undefined
      : `${label} must be at least ${limit} characters.`;

export const maxLength =
  (limit: number, label: string): Validator<string> =>
  (value) =>
    value.length <= limit ? undefined : `${label} must be ${limit} characters or fewer.`;

export const pattern =
  (regex: RegExp, message: string): Validator<string> =>
  (value) =>
    // An empty value is `required`'s business, not the pattern's - otherwise every
    // optional field reports a format error while untouched.
    !value || regex.test(value) ? undefined : message;

export const combine =
  (...validators: Validator<string>[]): Validator<string> =>
  (value, all) => {
    for (const validate of validators) {
      const error = validate(value, all);
      // First failure only. A field listing three simultaneous complaints is noise.
      if (error) return error;
    }
    return undefined;
  };

/* ------------------------------------------------------------------- hook */

export function useForm<K extends string>(fields: Record<K, FieldConfig>) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, (f as FieldConfig).initial])),
  );
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [formError, setFormError] = useState<string | undefined>();

  const validateField = useCallback(
    (name: string, value: string, all: Record<string, string>) => {
      const config = (fields as Record<string, FieldConfig>)[name];
      return config?.validate?.(value, all);
    },
    [fields],
  );

  const setValue = useCallback(
    (name: string, value: string) => {
      setValues((current) => {
        const next = { ...current, [name]: value };
        // Re-validate while typing only once the field is already showing an error: at
        // that point the user is fixing it and wants to watch it clear.
        setErrors((currentErrors) =>
          currentErrors[name] === undefined
            ? currentErrors
            : { ...currentErrors, [name]: validateField(name, value, next) },
        );
        return next;
      });
    },
    [validateField],
  );

  const blurField = useCallback(
    (name: string) => {
      setTouched((current) => ({ ...current, [name]: true }));
      setErrors((current) => ({ ...current, [name]: validateField(name, values[name], values) }));
    },
    [validateField, values],
  );

  const validateAll = useCallback(() => {
    const next: Record<string, string | undefined> = {};
    for (const name of Object.keys(fields)) {
      next[name] = validateField(name, values[name], values);
    }
    setErrors(next);
    setTouched(Object.fromEntries(Object.keys(fields).map((k) => [k, true])));
    return Object.values(next).every((e) => e === undefined);
  }, [fields, validateField, values]);

  /** Maps a server validation failure back onto the fields that caused it. */
  const applyServerError = useCallback((error: unknown) => {
    if (!(error instanceof ApiRequestError)) {
      setFormError(
        error instanceof Error ? error.message : "Something went wrong. Please try again.",
      );
      return;
    }

    // FastAPI's 422 shape: {detail: [{loc: ["body", "field"], msg: "..."}]}
    const detail = (error.body as unknown as { detail?: unknown }).detail;
    if (Array.isArray(detail)) {
      const mapped: Record<string, string> = {};
      for (const item of detail) {
        const loc = (item as { loc?: unknown[] }).loc;
        const msg = (item as { msg?: string }).msg;
        const field = Array.isArray(loc) ? String(loc[loc.length - 1]) : undefined;
        if (field && msg) mapped[field] = msg;
      }
      if (Object.keys(mapped).length > 0) {
        setErrors((current) => ({ ...current, ...mapped }));
        setTouched((current) => ({
          ...current,
          ...Object.fromEntries(Object.keys(mapped).map((k) => [k, true])),
        }));
        return;
      }
    }
    setFormError(error.body.message || "That could not be saved.");
  }, []);

  const reset = useCallback(() => {
    setValues(
      Object.fromEntries(
        Object.entries(fields).map(([k, f]) => [k, (f as FieldConfig).initial]),
      ),
    );
    setErrors({});
    setTouched({});
    setSubmitAttempted(false);
    setFormError(undefined);
  }, [fields]);

  const visibleErrors = useMemo(
    () =>
      Object.entries(errors)
        .filter(([name, error]) => error && (touched[name] || submitAttempted))
        .map(([name, error]) => ({
          name,
          label: (fields as Record<string, FieldConfig>)[name]?.label ?? name,
          error: error as string,
        })),
    [errors, touched, submitAttempted, fields],
  );

  return {
    values,
    errors,
    touched,
    submitting,
    submitAttempted,
    formError,
    visibleErrors,
    setValue,
    blurField,
    validateAll,
    applyServerError,
    reset,
    setSubmitting,
    setSubmitAttempted,
    setFormError,
    field: (name: K) => ({
      name,
      value: values[name],
      error: touched[name] || submitAttempted ? errors[name] : undefined,
      onChange: (v: string) => setValue(name, v),
      onBlur: () => blurField(name),
    }),
  };
}

/* -------------------------------------------------------------- components */

export interface FieldProps {
  name: string;
  label: string;
  value: string;
  error?: string;
  onChange: (value: string) => void;
  onBlur: () => void;
  hint?: string;
  type?: string;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
  options?: { value: string; label: string }[];
  /** Renders a <textarea> instead of a single-line <input> - for prose fields like a
   *  promotion reason, where a one-line box would just scroll the important part away. */
  multiline?: boolean;
  rows?: number;
}

export function Field({
  name,
  label,
  value,
  error,
  onChange,
  onBlur,
  hint,
  type = "text",
  placeholder,
  required: isRequired,
  disabled,
  options,
  multiline,
  rows = 4,
}: FieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  // Both are referenced, so the description and the error are read together rather than
  // the error replacing guidance the user still needs.
  const describedBy = [hint ? hintId : null, error ? errorId : null]
    .filter(Boolean)
    .join(" ");

  const shared = {
    id,
    name,
    value,
    disabled,
    "aria-invalid": error ? (true as const) : undefined,
    "aria-describedby": describedBy || undefined,
    "aria-required": isRequired || undefined,
    onChange: (e: { target: { value: string } }) => onChange(e.target.value),
    onBlur,
  };

  return (
    <div className={`field${error ? " field-invalid" : ""}`}>
      <label htmlFor={id}>
        {label}
        {isRequired && (
          <span className="field-required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </label>
      {hint && (
        <p className="field-hint" id={hintId}>
          {hint}
        </p>
      )}
      {options ? (
        <select {...shared}>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : multiline ? (
        <textarea {...shared} placeholder={placeholder} rows={rows} />
      ) : (
        <input {...shared} type={type} placeholder={placeholder} />
      )}
      {error && (
        <p className="field-error" id={errorId}>
          <span aria-hidden="true">✕ </span>
          {error}
        </p>
      )}
    </div>
  );
}

/** Lists every invalid field, focusable and linked.
 *
 *  Scrolling to the first error is trivial with a mouse and miserable without. This is
 *  focused on failed submit so a screen-reader user hears what went wrong immediately,
 *  rather than tabbing the whole form to find out.
 */
export function ErrorSummary({
  errors,
  formError,
}: {
  errors: { name: string; label: string; error: string }[];
  formError?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  if (errors.length === 0 && !formError) return null;

  return (
    <div className="error-summary" role="alert" tabIndex={-1} ref={ref}>
      <strong>
        {formError
          ? "This could not be saved"
          : `There ${errors.length === 1 ? "is 1 problem" : `are ${errors.length} problems`} with this form`}
      </strong>
      {formError && <p>{formError}</p>}
      {errors.length > 0 && (
        <ul>
          {errors.map((e) => (
            <li key={e.name}>
              <a href={`#${e.name}`} onClick={(ev) => focusField(ev, e.name)}>
                {e.label}: {e.error}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function focusField(event: { preventDefault: () => void }, name: string) {
  event.preventDefault();
  const element = document.querySelector<HTMLElement>(`[name="${name}"]`);
  element?.focus();
  element?.scrollIntoView({ block: "center", behavior: "smooth" });
}

export function FormActions({
  submitting,
  submitLabel,
  onCancel,
  disabled,
}: {
  submitting: boolean;
  submitLabel: string;
  onCancel?: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="form-actions">
      {/* Disabled only while submitting, never because the form is invalid: a submit
          button that does nothing and explains nothing is the single most common way to
          strand someone in a form. Clicking it shows them what is wrong. */}
      <button type="submit" disabled={submitting || disabled}>
        {submitting ? "Saving…" : submitLabel}
      </button>
      {onCancel && (
        <button type="button" className="btn-quiet" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      )}
    </div>
  );
}

export function onSubmitHandler(
  validateAll: () => boolean,
  setSubmitAttempted: (v: boolean) => void,
  run: () => void | Promise<void>,
) {
  return async (event: FormEvent) => {
    event.preventDefault();
    setSubmitAttempted(true);
    if (!validateAll()) {
      document.querySelector<HTMLElement>(".error-summary")?.focus();
      return;
    }
    await run();
  };
}
