import React from 'react';
import type { ColorValue } from 'react-native';
import { Tabs } from 'expo-router';
import { useColors } from '../../src/theme/colors';
import { AccountIcon, DashboardIcon as HomeIcon, type IconProps } from '../../src/components/Icons';

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
      initialRouteName="home"
      screenOptions={{
        tabBarActiveTintColor: colors.rose,
        tabBarInactiveTintColor: colors.faint,
        tabBarStyle: { backgroundColor: colors.well, borderTopColor: colors.hair },
        headerStyle: { backgroundColor: colors.well },
        headerTintColor: colors.text,
      }}
    >
      {/* Each tab is now a nested Stack (home/_layout.tsx, settings/_layout.tsx) that
       * renders its own header - same "avoid a double header" reason the old `incidents`
       * tab already used `headerShown: false` for. */}
      <Tabs.Screen name="home" options={{ title: 'Home', headerShown: false, tabBarIcon: tabIcon(HomeIcon) }} />
      <Tabs.Screen name="settings" options={{ title: 'Settings', headerShown: false, tabBarIcon: tabIcon(AccountIcon) }} />
    </Tabs>
  );
}
