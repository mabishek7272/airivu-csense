import React from 'react';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { AuthProvider, useAuth } from '../src/auth/AuthContext';
import { LoadingState } from '../src/components/States';
import { BrandProvider } from '../src/theme/BrandProvider';

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
  return (
    <AuthProvider>
      {/* Inside AuthProvider - BrandProvider reads useAuth() to know when a real session
          exists (so it can fetch the tenant's own effective branding, replacing whatever
          the pre-login slug implied). */}
      <BrandProvider>
        {/* "light" (light icons/text), not "auto" - the app has one theme now (the dark
            "Technical Atmosphere" canvas), not an OS-follows-light-or-dark split, so the
            status bar content should always be light, never auto-flip to dark icons on a
            dark background. */}
        <StatusBar style="light" />
        <RootLayoutNav />
      </BrandProvider>
    </AuthProvider>
  );
}
