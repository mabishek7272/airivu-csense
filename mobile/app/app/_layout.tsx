import React from 'react';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { AuthProvider, useAuth } from '../src/auth/AuthContext';
import { LoadingState } from '../src/components/States';

function RootLayoutNav() {
  const { isLoggedIn } = useAuth();

  if (isLoggedIn === null) {
    // Still checking SecureStore for an existing session - see AuthContext's own
    // `isLoggedIn` docstring for why this is a distinct third state from `false`.
    return <LoadingState label="Starting up…" />;
  }

  // `Stack.Protected` (expo-router's own auth-gating primitive) mounts exactly one of
  // these two branches based on `guard`, and re-navigates automatically when it flips -
  // logging in from src/auth/AuthContext.tsx's `login()` (which sets isLoggedIn: true)
  // is what actually moves the user off the login screen, not an explicit
  // `router.replace()` call from the login screen itself.
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Protected guard={isLoggedIn}>
        <Stack.Screen name="(tabs)" />
      </Stack.Protected>
      <Stack.Protected guard={!isLoggedIn}>
        <Stack.Screen name="login" />
      </Stack.Protected>
    </Stack>
  );
}

export default function RootLayout() {
  return (
    <AuthProvider>
      {/* "light" (light icons/text), not "auto" - the app has one theme now (the dark
          "Technical Atmosphere" canvas), not an OS-follows-light-or-dark split, so the
          status bar content should always be light, never auto-flip to dark icons on a
          dark background. */}
      <StatusBar style="light" />
      <RootLayoutNav />
    </AuthProvider>
  );
}
