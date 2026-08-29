/** State and classification always render as text, never colour alone.
 *  Same rule as frontend/customer-crm/src/components/Badges.tsx - colour-blind operators
 *  are common, and this is a product where mistaking a biometric model for a standard one
 *  has legal consequences, not just a wrong-looking UI. */

const STATE_CLASS: Record<string, string> = {
  production: "badge-low",
  staging: "badge-info",
  validated: "badge-info",
  validating: "badge-medium",
  uploaded: "badge-medium",
  deprecated: "badge-neutral",
  revoked: "badge-critical",
  // Pipeline version states (backend/admin_api/app/api/pipelines.py) share this badge -
  // "deprecated" above already covers both registries' identical use of the word.
  published: "badge-low",
  draft: "badge-medium",
};

export function StateBadge({ state }: { state: string }) {
  return (
    <span className={`badge ${STATE_CLASS[state] ?? "badge-neutral"}`}>
      <span className="visually-hidden">State: </span>
      {state}
    </span>
  );
}

export function ClassificationBadge({ classification }: { classification: string }) {
  if (classification === "biometric") {
    // Not just another badge colour: an icon prefix plus the word itself, so it reads
    // distinctly even to someone scanning quickly, not only to someone reading closely.
    return (
      <span
        className="badge badge-critical"
        title="Facial recognition is outside the approved release-one scope and carries additional legal obligations."
      >
        ⚠ Biometric
      </span>
    );
  }
  return <span className="badge badge-neutral">{classification}</span>;
}
