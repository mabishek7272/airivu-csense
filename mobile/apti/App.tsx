// mobile/apti/App.tsx
import { MaterialCommunityIcons } from '@expo/vector-icons';
import { useFonts, Inter_400Regular, Inter_500Medium, Inter_600SemiBold } from '@expo-google-fonts/inter';
import { StatusBar } from 'expo-status-bar';
import React, { useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { FaceGalleryProvider } from './src/FaceGalleryContext';
import { DetectScreen } from './src/screens/DetectScreen';
import { HistoryScreen } from './src/screens/HistoryScreen';
import { HomeScreen } from './src/screens/HomeScreen';
import { LoginScreen } from './src/screens/LoginScreen';
import { MonitorScreen } from './src/screens/MonitorScreen';
import { RegisterScreen } from './src/screens/RegisterScreen';
import { color, fontHeading } from './src/theme';

type Screen = 'home' | 'detect' | 'history' | 'register' | 'monitor';
type TabScreen = 'home' | 'detect' | 'history';

const TABS: { screen: TabScreen; label: string; icon: string }[] = [
  { screen: 'home', label: 'Home', icon: 'home-outline' },
  { screen: 'detect', label: 'Detect', icon: 'magnify-scan' },
  { screen: 'history', label: 'History', icon: 'history' },
];

export default function App() {
  const [fontsLoaded] = useFonts({ Inter_400Regular, Inter_500Medium, Inter_600SemiBold });
  if (!fontsLoaded) {
    return (
      <View style={styles.fontLoading}>
        <ActivityIndicator color={color.accent} />
      </View>
    );
  }
  return (
    <SafeAreaProvider>
      <FaceGalleryProvider>
        <Root />
      </FaceGalleryProvider>
    </SafeAreaProvider>
  );
}

function Root() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [screen, setScreen] = useState<Screen>('home');
  const isTabScreen = loggedIn && (screen === 'home' || screen === 'detect' || screen === 'history');

  if (!loggedIn) {
    return (
      <SafeAreaView style={styles.root} edges={['top', 'bottom']}>
        <StatusBar style="dark" />
        <View style={styles.body}>
          <LoginScreen onDone={() => setLoggedIn(true)} />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.root} edges={['top', 'bottom']}>
      <StatusBar style="dark" />
      <View style={styles.body}>
        {screen === 'home' && (
          <HomeScreen
            onRegister={() => setScreen('register')}
            onDetect={() => setScreen('detect')}
            onMonitor={() => setScreen('monitor')}
          />
        )}
        {screen === 'detect' && <DetectScreen />}
        {screen === 'history' && <HistoryScreen />}
        {screen === 'register' && <RegisterScreen onDone={() => setScreen('home')} onBack={() => setScreen('home')} />}
        {screen === 'monitor' && <MonitorScreen onBack={() => setScreen('home')} />}
      </View>

      {isTabScreen && (
        <View style={styles.tabBar}>
          {TABS.map((tab) => {
            const active = tab.screen === screen;
            return (
              <Pressable
                key={tab.screen}
                onPress={() => setScreen(tab.screen)}
                style={styles.tabItem}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
              >
                <MaterialCommunityIcons name={tab.icon as never} size={22} color={active ? color.accent : color.neutral500} />
                <Text style={[styles.tabLabel, active && { color: color.accent, fontFamily: fontHeading }]}>{tab.label}</Text>
              </Pressable>
            );
          })}
        </View>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fontLoading: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: color.bg },
  root: { backgroundColor: color.bg, flex: 1 },
  body: { flex: 1 },
  tabBar: {
    flexDirection: 'row', borderTopWidth: 1, borderTopColor: color.divider,
    backgroundColor: color.surface, paddingTop: 8, paddingBottom: 4,
  },
  tabItem: { flex: 1, alignItems: 'center', gap: 2, paddingVertical: 4 },
  tabLabel: { fontSize: 11, color: color.neutral500 },
});
