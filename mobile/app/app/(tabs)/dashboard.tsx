import React from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useDashboard } from '../../src/hooks/useDashboard';
import { LoadingState, ErrorState } from '../../src/components/States';
import { useColors } from '../../src/theme/colors';

function Tile({ label, value }: { label: string; value: number }) {
  const colors = useColors();
  return (
    <View style={[styles.tile, { backgroundColor: colors.well, borderColor: colors.border }]}>
      <Text style={[styles.tileValue, { color: colors.text }]}>{value}</Text>
      <Text style={[styles.tileLabel, { color: colors.textMuted }]}>{label}</Text>
    </View>
  );
}

export default function DashboardScreen() {
  const colors = useColors();
  const { data, loading, error, refresh } = useDashboard();

  if (loading && !data) return <LoadingState label="Loading dashboard…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  return (
    <View style={[styles.root, { backgroundColor: colors.canvas }]}>
      <View
        pointerEvents="none"
        style={[styles.glow, styles.glowViolet, { backgroundColor: colors.violetGlow }]}
      />
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.accent} />}
      >
        <View style={styles.grid}>
          <Tile label="Sites" value={data.sites_count} />
          <Tile label="Cameras" value={data.cameras_count} />
          <Tile label="Open incidents" value={data.incidents_open_count} />
          <Tile label="Total incidents" value={data.incidents_total_count} />
          <Tile label="Team members" value={data.team_members_count} />
        </View>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  // Approximates the mockup's radial glow behind the dashboard - RN has no CSS
  // radial-gradient, so this is a soft, low-opacity circle rather than a true fade.
  glow: { position: 'absolute', width: 420, height: 420, borderRadius: 210, opacity: 0.1 },
  glowViolet: { top: -140, left: -140 },
  content: { padding: 16 },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: 12 },
  tile: {
    borderRadius: 10,
    borderWidth: 1,
    flexBasis: '47%',
    flexGrow: 1,
    padding: 16,
  },
  tileValue: { fontSize: 28, fontWeight: '700' },
  tileLabel: { fontSize: 13, marginTop: 4 },
});
