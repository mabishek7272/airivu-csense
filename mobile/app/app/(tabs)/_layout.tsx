import React from 'react';
import type { ColorValue } from 'react-native';
import { Tabs } from 'expo-router';
import { useColors } from '../../src/theme/colors';
import { AccountIcon, CameraIcon, DashboardIcon, IncidentIcon, type IconProps } from '../../src/components/Icons';

export default function TabsLayout() {
  const colors = useColors();
  // react-navigation's own tabBarIcon signature types `color` as `ColorValue`, which
  // covers platform-native "opaque" colour objects (e.g. Android's PlatformColor) as well
  // as plain hex strings - Icons.tsx's `IconProps.color` only accepts the latter, since
  // react-native-svg's `stroke`/`fill` need a real string. `tabBarActiveTintColor`/
  // `tabBarInactiveTintColor` above are always set to plain hex strings from `colors`
  // here, never a platform colour, so the cast is accurate for how this app actually
  // calls it - not a blind `as string` covering a real possible mismatch.
  const tabIcon = (Icon: (props: IconProps) => React.ReactElement) => {
    function TabIcon({ color }: { color: ColorValue; focused: boolean; size: number }) {
      return <Icon size={22} color={color as string} />;
    }
    return TabIcon;
  };

  return (
    <Tabs
      initialRouteName="dashboard"
      screenOptions={{
        tabBarActiveTintColor: colors.rose,
        tabBarInactiveTintColor: colors.faint,
        tabBarStyle: { backgroundColor: colors.well, borderTopColor: colors.hair },
        headerStyle: { backgroundColor: colors.well },
        headerTintColor: colors.text,
      }}
    >
      <Tabs.Screen name="dashboard" options={{ title: 'Dashboard', tabBarIcon: tabIcon(DashboardIcon) }} />
      {/* Its own Stack (incidents/_layout.tsx) renders the header for both the list and
       * detail screens - the tab bar's header would otherwise show twice. */}
      <Tabs.Screen
        name="incidents"
        options={{ title: 'Incidents', headerShown: false, tabBarIcon: tabIcon(IncidentIcon) }}
      />
      <Tabs.Screen name="cameras" options={{ title: 'Cameras', tabBarIcon: tabIcon(CameraIcon) }} />
      <Tabs.Screen name="account" options={{ title: 'Account', tabBarIcon: tabIcon(AccountIcon) }} />
    </Tabs>
  );
}
