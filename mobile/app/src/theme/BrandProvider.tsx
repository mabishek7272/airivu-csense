import Constants from 'expo-constants';
import React, { useContext, useEffect, useState } from 'react';
import { fetchEffectiveBranding, fetchPublicBranding, type PublicBranding } from '../api/tenant';
import { useAuth } from '../auth/AuthContext';
import { BrandContext, palette, type BrandState, type Palette } from './colors';

/** `BrandContext` itself is defined in `colors.ts`, not here - see that file's own
 * comment for why: it keeps the dependency one-directional (this file imports
 * `palette`/`Palette`/`BrandContext` from `colors.ts`; `colors.ts` imports nothing back
 * from here), which is what avoids the circular-import risk the plan called out. */

function withOverrides(base: Palette, b: PublicBranding): Palette {
  return {
    ...base,
    // `color_accent` is branding's own *second* colour, not a literal "hover" concept -
    // but `accentHover` is the palette's only other accent-family field, so deriving it
    // from `color_accent` (falling back to `color_primary`, then the base palette's own
    // hover shade) is the sensible mapping: a brand's two supplied colours become the
    // app's "accent" and "accent, alternate/emphasized" rather than leaving
    // `accentHover` stuck on the default pink when `accent` itself has been overridden.
    accent: b.color_primary ?? base.accent,
    accentHover: b.color_accent ?? b.color_primary ?? base.accentHover,
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
      .catch(() => {
        // No branding for this slug, or unreachable - keep the static fallback palette
        // already in state rather than leaving the screen in some half-updated state.
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
      .catch(() => {});
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
