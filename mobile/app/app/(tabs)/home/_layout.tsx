import React from 'react';
import { Stack } from 'expo-router';
import { useColors } from '../../../src/theme/colors';

export default function HomeStackLayout() {
  const colors = useColors();
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.text,
      }}
    >
      {/* index (the 3-box screen) needs no title override - its own content already
       * shows the brand name as an in-content heading, the same way dashboard.tsx's
       * content used to render un-headered while the (then-flat) tab supplied the
       * header for free. No explicit Stack.Screen entry needed here; the default
       * applies. */}
      <Stack.Screen name="cameras" options={{ headerShown: false }} />
      <Stack.Screen name="alerts" options={{ headerShown: false }} />
      {/* "detections" (Task 15's job) has no screen file yet - deliberately not declared
       * here; see this task's own report for the "link to a not-yet-built route"
       * verification. */}
    </Stack>
  );
}
