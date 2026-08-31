import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useColors } from '../theme/colors';

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  const colors = useColors();
  return (
    <View style={styles.center}>
      <ActivityIndicator color={colors.accent} size="large" />
      <Text style={[styles.muted, { color: colors.textMuted }]}>{label}</Text>
    </View>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const colors = useColors();
  return (
    <View style={styles.center}>
      <Text style={[styles.title, { color: colors.critical }]}>Something went wrong</Text>
      <Text style={[styles.muted, { color: colors.textMuted, textAlign: 'center' }]}>{message}</Text>
      {onRetry ? (
        <Pressable
          onPress={onRetry}
          style={[styles.button, { borderColor: colors.accent }]}
          accessibilityRole="button"
        >
          <Text style={{ color: colors.accent, fontWeight: '600' }}>Try again</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

export function EmptyState({ message }: { message: string }) {
  const colors = useColors();
  return (
    <View style={styles.center}>
      <Text style={[styles.muted, { color: colors.textMuted, textAlign: 'center' }]}>{message}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  center: {
    alignItems: 'center',
    flex: 1,
    gap: 12,
    justifyContent: 'center',
    padding: 24,
  },
  title: {
    fontSize: 16,
    fontWeight: '600',
  },
  muted: {
    fontSize: 14,
  },
  button: {
    borderRadius: 8,
    borderWidth: 1,
    marginTop: 4,
    paddingHorizontal: 16,
    paddingVertical: 8,
  },
});
