import React from 'react';
import { FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useCameras } from '../../src/hooks/useCameras';
import { LoadingState, ErrorState, EmptyState } from '../../src/components/States';
import { useColors } from '../../src/theme/colors';
import type { CameraOut } from '../../src/api/types';

/** Same status→tone mapping as frontend/customer-crm/src/styles.css's own
 * `.pill-ready/.pill-online` etc - camera status isn't a Severity, so it gets its own
 * small map here rather than reusing SeverityBadge's vocabulary. */
function statusTone(colors: ReturnType<typeof useColors>, status: string) {
  if (['ready', 'online'].includes(status)) return { bg: colors.lowSubtle, fg: colors.low };
  if (['provisioning', 'pending', 'enrolled'].includes(status)) return { bg: colors.mediumSubtle, fg: colors.medium };
  return { bg: colors.surfaceSunken, fg: colors.textMuted };
}

function CameraRow({ camera }: { camera: CameraOut }) {
  const colors = useColors();
  const tone = statusTone(colors, camera.status);
  return (
    <View style={[styles.row, { borderBottomColor: colors.border }]}>
      <View style={styles.rowHeader}>
        <Text style={[styles.rowTitle, { color: colors.text }]} numberOfLines={1}>
          {camera.name}
        </Text>
        <View style={[styles.pill, { backgroundColor: tone.bg }]}>
          <Text style={{ color: tone.fg, fontSize: 12, fontWeight: '600', textTransform: 'capitalize' }}>
            {camera.status}
          </Text>
        </View>
      </View>
      <Text style={[styles.rowMeta, { color: colors.textMuted }]}>
        {camera.site_name ?? 'Unassigned site'} · {camera.code}
      </Text>
      {camera.last_error ? (
        <Text style={[styles.rowMeta, { color: colors.critical }]} numberOfLines={2}>
          {camera.last_error}
        </Text>
      ) : null}
    </View>
  );
}

export default function CamerasScreen() {
  const colors = useColors();
  const { data, loading, error, refresh } = useCameras();

  if (loading && data.length === 0) return <LoadingState label="Loading cameras…" />;
  if (error && data.length === 0) return <ErrorState message={error} onRetry={refresh} />;
  if (data.length === 0) return <EmptyState message="No cameras yet." />;

  return (
    <FlatList
      style={{ backgroundColor: colors.surface }}
      data={data}
      keyExtractor={(item) => item.id}
      renderItem={({ item }) => <CameraRow camera={item} />}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.accent} />}
    />
  );
}

const styles = StyleSheet.create({
  row: { borderBottomWidth: 1, padding: 16, gap: 4 },
  rowHeader: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  rowTitle: { fontSize: 15, fontWeight: '600', flexShrink: 1 },
  rowMeta: { fontSize: 12 },
  pill: { borderRadius: 12, paddingHorizontal: 8, paddingVertical: 2 },
});
