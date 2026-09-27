import React from 'react';
import { Stack } from 'expo-router';
import { useColors } from '../../../../src/theme/colors';

export default function DetectionsStackLayout() {
  const colors = useColors();
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.text,
      }}
    >
      <Stack.Screen name="index" options={{ title: 'Detections' }} />
      <Stack.Screen name="[id]" options={{ title: 'Detection' }} />
    </Stack>
  );
}
