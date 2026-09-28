// Per-weight subpath imports, not the package's top-level barrel - `@expo-google-fonts/
// inter`'s own index.js is one file of `require()` calls for every weight (100Thin
// through 900Black, plus every italic), so importing anything from it at all pulls
// every one of those font files into the bundle regardless of which weights this app
// actually loads below. Each weight's own subpath only `require()`s that one file.
import { Inter_400Regular } from '@expo-google-fonts/inter/400Regular';
import { Inter_500Medium } from '@expo-google-fonts/inter/500Medium';
import { Inter_600SemiBold } from '@expo-google-fonts/inter/600SemiBold';
import { Inter_700Bold } from '@expo-google-fonts/inter/700Bold';
import { useFonts } from 'expo-font';
import React from 'react';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { AuthProvider, useAuth } from '../src/auth/AuthContext';
import { LoadingState } from '../src/components/States';
import { BrandProvider } from '../src/theme/BrandProvider';
import { useColors } from '../src/theme/colors';

/** Luminance-based, not a per-brand light/dark lookup - `colors.surface` is the one
 * value that's always correct regardless of *why* a brand is light or dark (its own
 * base palette, or a dark-mode brand's org_branding override), so this reads the
 * resolved colour itself rather than trying to keep a second "is this brand light"
 * map in sync with `BRAND_PALETTE`/`BRAND_ALLOW_CANVAS_OVERRIDE`. Standard relative
 * luminance (ITU-R BT.601), thresholded at the common 0.5 midpoint. */
function isLightBackground(hex: string): boolean {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(hex);
  if (!m) return false;
  const [r, g, b] = [m[1], m[2], m[3]].map((h) => parseInt(h, 16));
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5;
}

function StatusBarForTheme() {
  // A light canvas (Apti, CSense) needs dark status bar content; the dark
  // "Technical Atmosphere" canvas (eAIgleye, 3RDi) needs light content - this was
  // previously hardcoded to "light" for every brand, which is wrong (invisible white
  // icons on a white status bar) for both light-themed brands introduced since.
  const colors = useColors();
  return <StatusBar style={isLightBackground(colors.surface) ? 'dark' : 'light'} />;
}

function RootLayoutNav() {
  const { authStage } = useAuth();

  if (authStage === 'checking') {
    // Still checking SecureStore for an existing session - see AuthContext's own
    // `AuthStage` docstring for why this is a distinct state from loggedIn/loggedOut.
    return <LoadingState label="Starting up…" />;
  }

  // `Stack.Protected` (expo-router's own auth-gating primitive) mounts exactly one of
  // these three branches based on `guard`, and re-navigates automatically when it
  // flips - logging in from src/auth/AuthContext.tsx's `login()` (which sets
  // authStage: 'mfaRequired' or 'loggedIn') is what actually moves the user off the
  // login screen, not an explicit `router.replace()` call from the login screen
  // itself; the same is true for `verifyMfa()`/`cancelMfa()` moving the user off the
  // mfa screen. Exactly one guard is ever true at a time (authStage is a single
  // three-way enum), so exactly one branch is ever mounted.
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Protected guard={authStage === 'loggedIn'}>
        <Stack.Screen name="(tabs)" />
      </Stack.Protected>
      <Stack.Protected guard={authStage === 'mfaRequired'}>
        <Stack.Screen name="mfa" />
      </Stack.Protected>
      <Stack.Protected guard={authStage === 'loggedOut'}>
        <Stack.Screen name="login" />
      </Stack.Protected>
    </Stack>
  );
}

export default function RootLayout() {
  // Gated the same way the auth-check loading state already is (a distinct "not ready
  // yet" render, not a flash of un-fonted text before this resolves) - `useFonts`
  // resolves fast (bundled assets, not a network fetch) but is still genuinely async on
  // first mount. Still rendered inside AuthProvider/BrandProvider (not returned bare
  // above them) so this loading state itself picks up the real brand's colours
  // immediately, rather than the context's own defensive fallback palette.
  const [fontsLoaded] = useFonts({ Inter_400Regular, Inter_500Medium, Inter_600SemiBold, Inter_700Bold });

  return (
    <AuthProvider>
      {/* Inside AuthProvider - BrandProvider reads useAuth() to know when a real session
          exists (so it can fetch the tenant's own effective branding, replacing whatever
          the pre-login slug implied). */}
      <BrandProvider>
        <StatusBarForTheme />
        {fontsLoaded ? <RootLayoutNav /> : <LoadingState label="Starting up…" />}
      </BrandProvider>
    </AuthProvider>
  );
}
