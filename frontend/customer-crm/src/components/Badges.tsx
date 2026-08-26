import type { IncidentStatus, Severity } from "../api/types";

/** Severity and status always render their label as text, never colour alone.
 *  Colour-blind operators are common, and this is a product where mistaking a critical
 *  alert for an informational one has consequences. */

const SEVERITY_CLASS: Record<Severity, string> = {
  critical: "badge-critical",
  high: "badge-high",
  medium: "badge-medium",
  low: "badge-low",
  info: "badge-info",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={`badge ${SEVERITY_CLASS[severity] ?? "badge-neutral"}`}>
      <span className="visually-hidden">Severity: </span>
      {severity}
    </span>
  );
}

const STATUS_CLASS: Record<IncidentStatus, string> = {
  open: "badge-critical",
  escalated: "badge-critical",
  investigating: "badge-high",
  acknowledged: "badge-medium",
  resolved: "badge-low",
  dismissed: "badge-neutral",
};

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <span className={`badge ${STATUS_CLASS[status] ?? "badge-neutral"}`}>
      <span className="visually-hidden">Status: </span>
      {status}
    </span>
  );
}

export function CountBadge({ count, noun }: { count: number; noun: string }) {
  return (
    <span className="badge badge-neutral">
      {count} {noun}
      {count === 1 ? "" : "s"}
    </span>
  );
}
