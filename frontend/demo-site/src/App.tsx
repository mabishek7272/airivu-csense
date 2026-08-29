import { useEffect, useState } from "react";
import { ModelShowcase } from "./components/ModelShowcase";
import { CATEGORIES } from "./categories";

type Manifest = Record<string, string[]>;

// Two real inboxes, not a placeholder alias invented for this page - both as "to"
// recipients rather than a to/cc split, so either person can pick it up.
const CONTACT_EMAILS = ["aron.morgan@airivu.ai", "ak@irairf.com"];
const CONTACT_HREF = `mailto:${CONTACT_EMAILS.join(",")}?subject=${encodeURIComponent(
  "CSense demo",
)}`;

export default function App() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [failed, setFailed] = useState(false);
  const [activeId, setActiveId] = useState(CATEGORIES[0].id);

  useEffect(() => {
    let cancelled = false;
    fetch("/showcase/manifest.json")
      .then((res) => {
        if (!res.ok) throw new Error(`manifest fetch failed: ${res.status}`);
        return res.json() as Promise<Manifest>;
      })
      .then((data) => {
        if (!cancelled) setManifest(data);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Only show tabs for categories the build actually rendered frames for — a category
  // with no source footage yet (see build_demo_assets.py) simply doesn't appear, rather
  // than shipping an empty, broken-looking tab.
  const available = CATEGORIES.filter((c) => (manifest?.[c.id]?.length ?? 0) > 0);
  const active = available.find((c) => c.id === activeId) ?? available[0];

  return (
    <div className="page">
      <header className="hero">
        <p className="hero-eyebrow">AIRIVU CSense</p>
        <h1>See it detect, on real footage</h1>
        <p className="hero-sub">
          Every frame below is a real detection from a real camera — not a mockup. Pick a
          model to see what CSense finds.
        </p>
      </header>

      {failed && (
        <div className="notice">
          <p>Couldn&apos;t load the demo frames right now. Please refresh, or check back shortly.</p>
        </div>
      )}

      {!failed && !manifest && (
        <div className="notice" aria-busy="true" aria-live="polite">
          <p>Loading…</p>
        </div>
      )}

      {active && (
        <>
          <nav className="tabs" aria-label="Detection models">
            {available.map((category) => (
              <button
                key={category.id}
                type="button"
                className={category.id === active.id ? "tab tab-active" : "tab"}
                aria-current={category.id === active.id ? "page" : undefined}
                onClick={() => setActiveId(category.id)}
              >
                {category.title}
              </button>
            ))}
          </nav>

          <main>
            <ModelShowcase category={active} images={manifest![active.id] ?? []} />
          </main>
        </>
      )}

      <section className="cta" aria-labelledby="cta-heading">
        <h2 id="cta-heading">Want to see this on your own site?</h2>
        <p>Send over a few minutes of footage from a camera you already have, and we&apos;ll show you exactly what CSense finds on it.</p>
        <a className="cta-button" href={CONTACT_HREF}>
          Talk to us
        </a>
      </section>

      <footer className="footer">
        <p>AIRIVU CSense — on-site AI detection. This page runs no live model; every example is pre-rendered.</p>
      </footer>
    </div>
  );
}
