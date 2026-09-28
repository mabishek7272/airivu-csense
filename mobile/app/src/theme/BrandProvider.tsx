import Constants from 'expo-constants';
import React, { useContext, useEffect, useState } from 'react';
import { fetchEffectiveBranding, fetchPublicBranding, type PublicBranding } from '../api/tenant';
import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { BRAND_ALLOW_CANVAS_OVERRIDE, BRAND_PALETTE, BrandContext, palette, type BrandState, type Palette } from './colors';

/** `BrandContext` itself is defined in `colors.ts`, not here - see that file's own
 * comment for why: it keeps the dependency one-directional (this file imports
 * `palette`/`Palette`/`BrandContext` from `colors.ts`; `colors.ts` imports nothing back
 * from here), which is what avoids the circular-import risk the plan called out. */

const BRAND_SLUG = (Constants.expoConfig?.extra as { brandSlug?: string } | undefined)?.brandSlug ?? 'csense';
const BASE_PALETTE: Palette = BRAND_PALETTE[BRAND_SLUG] ?? palette;
const ALLOW_CANVAS_OVERRIDE = BRAND_ALLOW_CANVAS_OVERRIDE[BRAND_SLUG] ?? true;

/** A real local mark for this exact build's brand, used only until (or unless) a real
 * `org_branding`/`fetchEffectiveBranding` logo resolves. Metro's `require()` needs a
 * literal path per call - it cannot be built from the `BRAND_SLUG` variable - so this is
 * one entry per brand, not a dynamic lookup. Apti has real, distinct logo art
 * (`apti-logo.png`, tightly cropped - see Task 14's own splash-icon fix for why that
 * matters); CSense's is the new placeholder mark this reskin adds (`csense-logo.png`) -
 * eAIgleye/3RDi have no distinct mark yet (their own `icon.png` is still CSense's old
 * dark-theme default, matching their own placeholder-asset status, unchanged by this
 * reskin) so they fall back to that rather than claiming art that doesn't exist. */
const LOCAL_LOGOS: Record<string, number> = {
  apti: require('../../brand/apti/apti-logo.png'),
  csense: require('../../brand/csense/csense-logo.png'),
  eaigleye: require('../../brand/eaigleye/icon.png'),
  '3rdi': require('../../brand/3rdi/icon.png'),
};
/** Exported (not threaded through `BrandState`/context) because it never changes for
 * the lifetime of a build - it's a pure function of `BRAND_SLUG`, not something a
 * network fetch ever updates the way `logoUrl` is. A screen wanting "whichever logo is
 * best right now" uses `brand.logoUrl ?? BRAND_LOCAL_LOGO` directly. */
export const BRAND_LOCAL_LOGO: number | null = LOCAL_LOGOS[BRAND_SLUG] ?? null;

/** This build's own brand name, baked in at build time
 * (`app.config.ts`'s `BRAND_NAMES[VARIANT]`, surfaced here via `expo-constants` rather
 * than a second parallel name map to keep in sync). Used until a real branding fetch
 * resolves - some brands (CSense: confirmed via a live `GET /api/v1/public/branding/
 * csense` -> 404) have no `org_branding` row at all and never will, so this is not just
 * an instant-long fallback for those, it's the name that's shown, permanently. Falling
 * back further to an empty string only in the pathological case where the native build
 * config itself has no name (should not happen for a real build of this app). */
const LOCAL_NAME = Constants.expoConfig?.name ?? '';

function withOverrides(base: Palette, b: PublicBranding): Palette {
  return {
    ...base,
    // Matches the web CRM's own BrandProvider.tsx (frontend/customer-crm/src/branding/
    // BrandProvider.tsx, applyBranding()): a brand only ever supplies one primary
    // color, not two shades of it, so color_primary maps to accent/accentHover AND
    // the two fields real screens actually render as their primary-action/link
    // colours (`crimson`/`rose` - `login.tsx`'s button is `colors.crimson`, not
    // `colors.accent`; overriding only `accent*` left every real button/link
    // unbranded, which is exactly the bug that shipped in the first Apti build).
    accent: b.color_primary ?? base.accent,
    accentHover: b.color_primary ?? base.accentHover,
    rose: b.color_primary ?? base.rose,
    crimson: b.color_primary ?? base.crimson,
    // color_accent is the brand's own second colour, used for tinted/background
    // surfaces (the web CRM maps it to --accent-subtle) - not a hover state. The web
    // CRM sets that CSS custom property directly to the raw hex with no opacity
    // transform, so this mirrors that: color_accent's raw hex replaces the base
    // palette's rgba() tint outright rather than being blended into it.
    accentSubtle: b.color_accent ?? base.accentSubtle,
    // Background/canvas overrides only apply to brands whose real org_branding rows
    // are configured with a dark tone (see BRAND_ALLOW_CANVAS_OVERRIDE in colors.ts) -
    // applying one to a light-palette brand would silently turn its whole app dark
    // again the moment branding resolves. A light brand's backgrounds come from its
    // own light palette instead, same as canvas/surface/surfaceSunken already do
    // before this override runs.
    ...(ALLOW_CANVAS_OVERRIDE
      ? {
          canvas: b.color_canvas ?? base.canvas,
          surface: b.color_canvas ?? base.surface,
          surfaceSunken: b.color_canvas ?? base.surfaceSunken,
        }
      : {}),
  };
}

function toBrandState(b: PublicBranding): BrandState {
  return {
    colors: withOverrides(BASE_PALETTE, b),
    displayName: b.display_name,
    logoUrl: b.logo_url,
  };
}

export function BrandProvider({ children }: { children: React.ReactNode }) {
  // The build's own real, static brand name (LOCAL_NAME) - not a hardcoded literal
  // ("CSense" for every brand was the exact bug an earlier build shipped with), and not
  // blank either: CSense has no `org_branding` row at all (confirmed via a live
  // `GET /api/v1/public/branding/csense` -> 404) and never will, so a blank-until-fetch
  // default would be blank forever for that brand, not just for an instant. `logoUrl`
  // itself still starts `null` (a remote URL, only ever set by a real fetch) - the
  // build's own local mark (`BRAND_LOCAL_LOGO`) is a screen-level fallback, not part of
  // this state, since it never changes.
  const [state, setState] = useState<BrandState>({ colors: BASE_PALETTE, displayName: LOCAL_NAME, logoUrl: null });
  // AuthContext now exposes a three-way `authStage` enum (Task 12) instead of a boolean
  // `isLoggedIn` - `authStage === 'loggedIn'` is the equivalent "fetch effective
  // branding" trigger (deliberately NOT `'mfaRequired'` - no real session/tenant exists
  // yet at that stage, only an MFA ticket, so there is nothing to fetch branding for).
  const { authStage } = useAuth();
  const isLoggedIn = authStage === 'loggedIn';

  // Pre-login: resolve branding from the build-time slug (app.config.ts's own
  // `extra.brandSlug`, defaulting to 'csense' the same way that config does) via the
  // public, unauthenticated endpoint. Runs once on mount - this is what colors the
  // splash/login screen before any session exists.
  useEffect(() => {
    fetchPublicBranding(BRAND_SLUG)
      .then((b) => setState(toBrandState(b)))
      .catch((err) => {
        // A 404 (no branding configured for this slug) is the expected, common case -
        // not a real failure - matching the web CRM's own BrandProvider.tsx, which
        // checks err.status !== 404 the same way. Anything else (network failure,
        // unreachable API, a typo'd APP_VARIANT) is a genuine problem worth surfacing,
        // even though the screen still falls back to the static palette either way.
        if (!(err instanceof ApiError) || err.httpStatusCode !== 404) {
          console.warn('public branding lookup failed, falling back to default', err);
        }
      });
  }, []);

  // Post-login: the session's own organization always wins over whatever the pre-login
  // slug implied (see branding.py's own docstring on this trust boundary). Only runs
  // once a real session exists.
  useEffect(() => {
    if (isLoggedIn !== true) return;
    fetchEffectiveBranding()
      .then((b) => {
        if (!b) return; // unbranded tenant - keep whatever pre-login branding resolved.
        setState(toBrandState(b));
      })
      .catch((err) => {
        // Unlike the public lookup above, this authenticated endpoint's "no branding"
        // answer is a real `null` body, never a 404 - so there's no expected-error case
        // to special-case here. Any rejection is a genuine failure, matching the web
        // CRM's own unconditional warn on this same call.
        console.warn('effective branding lookup failed, keeping prior branding', err);
      });
  }, [isLoggedIn]);

  return <BrandContext.Provider value={state}>{children}</BrandContext.Provider>;
}

/** Full brand state (colors + display name + logo), for the few call sites that need
 * more than just the palette (e.g. a login screen showing the tenant's own name/logo).
 * Everything currently in the codebase reading only colors keeps using `useColors()` -
 * this is additive, not a replacement for it. */
export function useBrand(): BrandState {
  return useContext(BrandContext);
}
