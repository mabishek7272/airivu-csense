import React from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { Link } from 'expo-router';
import { useSites } from '../../../../src/hooks/useSites';
import { LoadingState, ErrorState, EmptyState } from '../../../../src/components/States';
import { useColors } from '../../../../src/theme/colors';
import type { SiteOut } from '../../../../src/api/types';

function statusTone(colors: ReturnType<typeof useColors>, status: string) {
  if (status === 'active') return { bg: colors.lowSubtle, fg: colors.low };
  if (['provisioning', 'pending'].includes(status)) return { bg: colors.mediumSubtle, fg: colors.medium };
  return { bg: colors.surfaceSunken, fg: colors.textMuted };
}

function SiteRow({ site }: { site: SiteOut }) {
  const colors = useColors();
  const tone = statusTone(colors, site.status);
  return (
    <Link href={`/(tabs)/home/sites/${site.id}`} asChild>
      <Pressable style={[styles.row, { borderBottomColor: colors.border }]} accessibilityRole="button">
        <View style={styles.rowHeader}>
          <Text style={[styles.rowTitle, { color: colors.text }]} numberOfLines={1}>
            {site.name}
          </Text>
          <View style={[styles.pill, { backgroundColor: tone.bg }]}>
            <Text style={{ color: tone.fg, fontSize: 12, textTransform: 'capitalize', fontFamily: 'Inter_600SemiBold' }}>
              {site.status}
            </Text>
          </View>
        </View>
        <Text style={[styles.rowMeta, { color: colors.textMuted }]}>
          {site.code} · {site.camera_count} camera{site.camera_count === 1 ? '' : 's'} · {site.device_count}{' '}
          device{site.device_count === 1 ? '' : 's'}
        </Text>
      </Pressable>
    </Link>
  );
}

export default function SitesScreen() {
  const colors = useColors();
  const { data, loading, error, refresh } = useSites();

  if (loading && data.length === 0) return <LoadingState label="Loading sites…" />;
  if (error && data.length === 0) return <ErrorState message={error} onRetry={refresh} />;
  if (data.length === 0) return <EmptyState message="No sites yet." />;

  return (
    <FlatList
      style={{ backgroundColor: colors.surface }}
      data={data}
      keyExtractor={(item) => item.id}
      renderItem={({ item }) => <SiteRow site={item} />}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.accent} />}
    />
  );
}

const styles = StyleSheet.create({
  row: { borderBottomWidth: 1, padding: 16, gap: 4 },
  rowHeader: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  rowTitle: { fontSize: 15, flexShrink: 1, fontFamily: 'Inter_600SemiBold' },
  rowMeta: { fontSize: 12, fontFamily: 'Inter_400Regular' },
  pill: { borderRadius: 12, paddingHorizontal: 8, paddingVertical: 2 },
});
