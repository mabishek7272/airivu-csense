import React from 'react';
import { Stack } from 'expo-router';
import { useColors } from '../../../src/theme/colors';

export default function IncidentsStackLayout() {
  const colors = useColors();
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.text,
      }}
    >
      <Stack.Screen name="index" options={{ title: 'Incidents' }} />
      <Stack.Screen name="[id]" options={{ title: 'Incident' }} />
    </Stack>
  );
}
