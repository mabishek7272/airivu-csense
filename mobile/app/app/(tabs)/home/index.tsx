import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useColors } from '../../../src/theme/colors';
import { useBrand } from '../../../src/theme/BrandProvider';

function Box({ title, onPress }: { title: string; onPress: () => void }) {
  const colors = useColors();
  return (
    <Pressable
      onPress={onPress}
      style={[styles.box, { backgroundColor: colors.surfaceSunken, borderColor: colors.border }]}
      accessibilityRole="button"
    >
      <Text style={{ color: colors.text, fontSize: 16, fontWeight: '600' }}>{title}</Text>
    </Pressable>
  );
}

export default function HomeScreen() {
  const router = useRouter();
  const brand = useBrand();
  const colors = useColors();
  return (
    <View style={[styles.root, { backgroundColor: colors.surface }]}>
      <Text style={{ color: colors.text, fontSize: 22, fontWeight: '700' }}>{brand.displayName}</Text>
      <Box title="Camera" onPress={() => router.push('/(tabs)/home/cameras')} />
      <Box title="Alerts" onPress={() => router.push('/(tabs)/home/alerts')} />
      {/* Detections screen is Task 15's job - route doesn't exist yet, this is a runtime
       * 404 (Unmatched Route) if tapped today, not a build/type error (typed routes
       * aren't enabled in app.config.ts, so `router.push` takes a plain string). */}
      <Box title="Detections" onPress={() => router.push('/(tabs)/home/detections')} />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, padding: 16, gap: 12 },
  box: { padding: 20, borderRadius: 10, borderWidth: 1 },
});
