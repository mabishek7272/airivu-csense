import Constants from 'expo-constants';
import React, { useContext, useEffect, useState } from 'react';
import { fetchEffectiveBranding, fetchPublicBranding, type PublicBranding } from '../api/tenant';
import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { BrandContext, palette, type BrandState, type Palette } from './colors';

/** `BrandContext` itself is defined in `colors.ts`, not here - see that file's own
 * comment for why: it keeps the dependency one-directional (this file imports
 * `palette`/`Palette`/`BrandContext` from `colors.ts`; `colors.ts` imports nothing back
 * from here), which is what avoids the circular-import risk the plan called out. */

function withOverrides(base: Palette, b: PublicBranding): Palette {
  return {
    ...base,
    // Matches the web CRM's own BrandProvider.tsx (frontend/customer-crm/src/branding/
    // BrandProvider.tsx, applyBranding()): a brand only ever supplies one primary
    // color, not two shades of it, so color_primary maps to BOTH accent and
    // accentHover, same value - not treating color_accent as a hover shade.
    accent: b.color_primary ?? base.accent,
    accentHover: b.color_primary ?? base.accentHover,
    // color_accent is the brand's own second colour, used for tinted/background
    // surfaces (the web CRM maps it to --accent-subtle) - not a hover state. The web
    // CRM sets that CSS custom property directly to the raw hex with no opacity
    // transform, so this mirrors that: color_accent's raw hex replaces the base
    // palette's rgba() tint outright rather than being blended into it.
    accentSubtle: b.color_accent ?? base.accentSubtle,
    canvas: b.color_canvas ?? base.canvas,
  };
}

function toBrandState(base: Palette, b: PublicBranding): BrandState {
  return {
    colors: withOverrides(base, b),
    displayName: b.display_name,
    logoUrl: b.logo_url,
  };
}

export function BrandProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<BrandState>({ colors: palette, displayName: 'CSense', logoUrl: null });
  // NOTE: this codebase's real AuthContext exposes `isLoggedIn: boolean | null`, NOT an
  // `authStage` enum (that three-way enum is introduced by a LATER task in this same
  // plan, Task 12 - not yet done at the time of writing). `isLoggedIn === true` is used
  // below as the "fetch effective branding" trigger; Task 12 will need to revisit this
  // dependency once `authStage` exists, but that's out of scope here.
  const { isLoggedIn } = useAuth();

  // Pre-login: resolve branding from the build-time slug (app.config.ts's own
  // `extra.brandSlug`, defaulting to 'csense' the same way that config does) via the
  // public, unauthenticated endpoint. Runs once on mount - this is what colors the
  // splash/login screen before any session exists.
  useEffect(() => {
    const slug = (Constants.expoConfig?.extra as { brandSlug?: string } | undefined)?.brandSlug ?? 'csense';
    fetchPublicBranding(slug)
      .then((b) => setState(toBrandState(palette, b)))
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
        setState(toBrandState(palette, b));
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
