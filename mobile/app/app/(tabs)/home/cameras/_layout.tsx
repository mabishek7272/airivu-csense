import React from 'react';
import { Stack } from 'expo-router';
import { useColors } from '../../../../src/theme/colors';

export default function CamerasStackLayout() {
  const colors = useColors();
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.text,
      }}
    >
      <Stack.Screen name="index" options={{ title: 'Cameras' }} />
      {/* "[id]" (camera detail) is Task 14's job - no screen file yet, deliberately not
       * declared here; see this task's own report for the "link to a not-yet-built
       * route" verification. */}
    </Stack>
  );
}
