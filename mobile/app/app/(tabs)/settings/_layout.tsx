import React from 'react';
import { Stack } from 'expo-router';
import { useColors } from '../../../src/theme/colors';

export default function SettingsStackLayout() {
  const colors = useColors();
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.text,
      }}
    >
      <Stack.Screen name="index" options={{ title: 'Settings' }} />
      {/* "profile"/"notifications" are Task 14+'s job - no screen files yet,
       * deliberately not declared here (same reasoning as home/_layout.tsx's
       * "detections" and cameras/_layout.tsx's "[id]"). */}
    </Stack>
  );
}
