import type { Evidence } from "../api/types";

/** Preference order for what to show. `annotated` first because the boxes are the point —
 *  a snapshot without them makes an operator guess which part of the frame fired.
 *  `original` is never auto-selected even when the caller is entitled to it: the unmasked
 *  frame should be a deliberate choice, not what a list happens to render. */
const DISPLAY_ORDER = ["annotated", "masked"] as const;

export function pickDisplayEvidence(evidence: Evidence[]): Evidence | null {
  for (const variant of DISPLAY_ORDER) {
    const match = evidence.find((item) => item.variant === variant && item.url);
    if (match) return match;
  }
  return null;
}

const VARIANT_LABEL: Record<string, string> = {
  annotated: "Annotated · faces masked",
  masked: "Faces masked",
  original: "Unmasked",
};

export function EvidenceThumb({
  evidence,
  alt,
}: {
  evidence: Evidence[];
  alt: string;
}) {
  const shown = pickDisplayEvidence(evidence);

  if (!shown) {
    return (
      <div className="thumb">
        <div className="thumb-empty">
          {evidence.length > 0
            ? "Snapshot unavailable"
            : "No snapshot captured for this detection"}
        </div>
      </div>
    );
  }

  return (
    <div className="thumb">
      {/* Presigned URLs expire, so a stale card must degrade to the placeholder rather
          than a broken-image icon. */}
      <img
        src={shown.url ?? ""}
        alt={alt}
        loading="lazy"
        onError={(event) => {
          const el = event.currentTarget;
          el.style.display = "none";
          el.insertAdjacentHTML(
            "afterend",
            '<div class="thumb-empty">Snapshot link expired — reload to view</div>',
          );
        }}
      />
      <span className="thumb-badge">{VARIANT_LABEL[shown.variant] ?? shown.variant}</span>
    </div>
  );
}
