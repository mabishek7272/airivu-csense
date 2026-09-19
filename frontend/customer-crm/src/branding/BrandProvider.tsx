import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { getEffectiveBranding, getPublicBranding, type Branding } from "../api/branding";
import { ApiRequestError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

interface BrandState {
  displayName: string | null;
  logoUrl: string | null;
  faviconUrl: string | null;
  colorPrimary: string | null;
  colorAccent: string | null;
  colorCanvas: string | null;
  /** True once branding has been resolved one way or the other (found, or confirmed
   *  absent) — lets the login page avoid a flash of the wrong wordmark before the
   *  first fetch settles. */
  resolved: boolean;
  /** False whenever a real brand (own or inherited) is showing; true for the default
   *  CSense look, either because no slug was in the URL or because nothing resolved. */
  isDefaultBrand: boolean;
  /** Was a brand slug present in the URL at all, independent of whether it resolved to
   *  a real brand. LoginPage uses this — not `isDefaultBrand` — to decide whether to
   *  hide self-registration: a stranger reaching a URL that was clearly meant to look
   *  like eAIgleye's own private login page should never see "create your own
   *  organization" there, even if the slug happens to be misspelled/unresolved. */
  hasSlug: boolean;
}

const DEFAULT_STATE: BrandState = {
  displayName: null,
  logoUrl: null,
  faviconUrl: null,
  colorPrimary: null,
  colorAccent: null,
  colorCanvas: null,
  resolved: false,
  isDefaultBrand: true,
  hasSlug: false,
};

const BrandContext = createContext<BrandState>(DEFAULT_STATE);

export function useBrand(): BrandState {
  return useContext(BrandContext);
}

function applyBranding(b: Branding | null) {
  // styles.css's :root block separates semantic aliases (--accent, --bg) from the raw
  // primitives they normally derive from (--rose, --crimson, --canvas) - but not every
  // component actually goes through the alias: button.primary's fill and a few other
  // rules reference --crimson/--rose directly (found by screenshotting a real branded
  // login page and noticing the "Sign in" button stayed the default color - overriding
  // --accent alone silently missed the single most visible brand touchpoint on the
  // page). So both layers are set together: the semantic aliases for anything that
  // does route through them, and the primitives directly for the handful of rules that
  // don't, using one supplied color for both --rose and --crimson (the theme uses them
  // as a lighter/fill pair of the same brand hue, and a brand only ever supplies one
  // primary color, not two shades of it). An inline style on :root always wins the
  // cascade over the stylesheet rule without fighting specificity the way injecting a
  // second <style> block would.
  const root = document.documentElement.style;
  if (b?.color_primary) {
    root.setProperty("--accent", b.color_primary);
    root.setProperty("--accent-hover", b.color_primary);
    root.setProperty("--rose", b.color_primary);
    root.setProperty("--crimson", b.color_primary);
  } else {
    root.removeProperty("--accent");
    root.removeProperty("--accent-hover");
    root.removeProperty("--rose");
    root.removeProperty("--crimson");
  }
  if (b?.color_accent) {
    root.setProperty("--accent-subtle", b.color_accent);
  } else {
    root.removeProperty("--accent-subtle");
  }
  if (b?.color_canvas) {
    root.setProperty("--bg", b.color_canvas);
    root.setProperty("--canvas", b.color_canvas);
  } else {
    root.removeProperty("--bg");
    root.removeProperty("--canvas");
  }

  // A branded org's tab title must carry zero AIRIVU/CSense identity, matching the
  // same "full white-label" decision applied to the login page and app shell — a
  // "powered by AIRIVU CSense" suffix here was a real leak, caught by production
  // screenshots (browser tabs are easy to overlook since the page body looks correct).
  document.title = b ? b.display_name : "AIRIVU CSense — Customer CRM";

  // No <link rel="icon"> exists in index.html by default — see the tag added there
  // with id="brand-favicon" as the element this always finds.
  const faviconEl = document.getElementById("brand-favicon") as HTMLLinkElement | null;
  if (faviconEl) {
    faviconEl.href = b?.favicon_url ?? "/favicon-default.svg";
  }
}

export function BrandProvider({ brandSlug, children }: { brandSlug: string | null; children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  // hasSlug is derived purely from the URL-detected slug and never changes for the
  // life of this provider (see main.tsx — the slug itself is read once, before mount).
  const [state, setState] = useState<BrandState>({ ...DEFAULT_STATE, hasSlug: brandSlug !== null });

  // Pre-login: resolve from the URL slug, if any. Runs once — the slug itself never
  // changes within a session (see main.tsx's own note on why).
  useEffect(() => {
    if (!brandSlug) {
      setState((s) => ({ ...s, resolved: true }));
      return;
    }
    let cancelled = false;
    getPublicBranding(brandSlug)
      .then((b) => {
        if (cancelled) return;
        setState({
          displayName: b.display_name, logoUrl: b.logo_url, faviconUrl: b.favicon_url,
          colorPrimary: b.color_primary, colorAccent: b.color_accent, colorCanvas: b.color_canvas,
          resolved: true, isDefaultBrand: false, hasSlug: true,
        });
      })
      .catch((err) => {
        if (cancelled) return;
        // A 404 (no such slug) is not an error condition here — it just means "no
        // brand for this path," the same as no slug being present at all. Anything
        // else (network failure) also falls back to the default look rather than
        // leaving the page stuck unresolved.
        if (!(err instanceof ApiRequestError) || err.status !== 404) {
          console.warn("public branding lookup failed, falling back to default", err);
        }
        setState((s) => ({ ...s, resolved: true }));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Post-login: the session's own organization always wins over whatever the
  // pre-login URL showed — this is the trust boundary the whole design rests on. Runs
  // again on every isAuthenticated flip (covers login, and a silent-refresh failure
  // that logs someone out reverting to the URL-derived or default look).
  useEffect(() => {
    if (!isAuthenticated) return;
    let cancelled = false;
    getEffectiveBranding()
      .then((b) => {
        if (cancelled) return;
        if (b === null) {
          setState((s) => ({ ...DEFAULT_STATE, resolved: true, isDefaultBrand: true, hasSlug: s.hasSlug }));
          return;
        }
        setState((s) => ({
          displayName: b.display_name, logoUrl: b.logo_url, faviconUrl: b.favicon_url,
          colorPrimary: b.color_primary, colorAccent: b.color_accent, colorCanvas: b.color_canvas,
          resolved: true, isDefaultBrand: false, hasSlug: s.hasSlug,
        }));
      })
      .catch((err) => {
        if (cancelled) return;
        console.warn("effective branding lookup failed, keeping prior branding", err);
      });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated]);

  useEffect(() => {
    applyBranding(
      state.isDefaultBrand
        ? null
        : {
            slug: brandSlug ?? "",
            display_name: state.displayName ?? "",
            logo_url: state.logoUrl,
            favicon_url: state.faviconUrl,
            color_primary: state.colorPrimary,
            color_accent: state.colorAccent,
            color_canvas: state.colorCanvas,
          },
    );
  }, [state, brandSlug]);

  return <BrandContext.Provider value={state}>{children}</BrandContext.Provider>;
}
