import React from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useDashboard } from '../../src/hooks/useDashboard';
import { LoadingState, ErrorState } from '../../src/components/States';
import { useColors } from '../../src/theme/colors';

function Tile({ label, value }: { label: string; value: number }) {
  const colors = useColors();
  return (
    <View style={[styles.tile, { backgroundColor: colors.surfaceSunken, borderColor: colors.border }]}>
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
    <ScrollView
      style={{ backgroundColor: colors.surface }}
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
  );
}

const styles = StyleSheet.create({
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
