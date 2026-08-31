import React from 'react';
import { Tabs } from 'expo-router';
import { useColors } from '../../src/theme/colors';

export default function TabsLayout() {
  const colors = useColors();
  return (
    <Tabs
      initialRouteName="dashboard"
      screenOptions={{
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.textMuted,
        tabBarStyle: { backgroundColor: colors.surface, borderTopColor: colors.border },
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.text,
      }}
    >
      <Tabs.Screen name="dashboard" options={{ title: 'Dashboard' }} />
      {/* Its own Stack (incidents/_layout.tsx) renders the header for both the list and
       * detail screens - the tab bar's header would otherwise show twice. */}
      <Tabs.Screen name="incidents" options={{ title: 'Incidents', headerShown: false }} />
      <Tabs.Screen name="cameras" options={{ title: 'Cameras' }} />
      <Tabs.Screen name="account" options={{ title: 'Account' }} />
    </Tabs>
  );
}
