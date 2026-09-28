import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { useLocalSearchParams } from 'expo-router';
import { useSiteDetail } from '../../../../src/hooks/useSiteDetail';
import { LoadingState, ErrorState } from '../../../../src/components/States';
import { useColors } from '../../../../src/theme/colors';

function Row({ label, value }: { label: string; value: string }) {
  const colors = useColors();
  return (
    <View style={styles.metaRow}>
      <Text style={[styles.metaLabel, { color: colors.textMuted }]}>{label}</Text>
      <Text style={[styles.metaValue, { color: colors.text }]}>{value}</Text>
    </View>
  );
}

/** No map view - `address`/`latitude`/`longitude` are shown as plain text, matching
 * this app's own established discipline of not pulling in a new native dependency for
 * a v1 screen (the camera live-view work made the same call for HLS playback). */
function formatAddress(address: Record<string, unknown> | null): string | null {
  if (!address) return null;
  const parts = Object.values(address).filter((v) => typeof v === 'string' && v.trim().length > 0);
  return parts.length > 0 ? parts.join(', ') : null;
}

export default function SiteDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const colors = useColors();
  const { data, loading, error, refresh } = useSiteDetail(id);

  if (loading && !data) return <LoadingState label="Loading site…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  const address = formatAddress(data.address);

  return (
    <ScrollView style={{ backgroundColor: colors.surface }} contentContainerStyle={styles.content}>
      <Text style={[styles.title, { color: colors.text }]}>{data.name}</Text>
      <Text style={[styles.subtitle, { color: colors.textMuted, textTransform: 'capitalize' }]}>
        {data.status} · {data.code}
      </Text>

      <View style={[styles.card, { borderColor: colors.hair, backgroundColor: colors.raised }]}>
        <Row label="Cameras" value={String(data.camera_count)} />
        <Row label="Devices" value={String(data.device_count)} />
        <Row label="Timezone" value={data.timezone} />
        {address ? <Row label="Address" value={address} /> : null}
        {data.latitude !== null && data.longitude !== null ? (
          <Row label="Coordinates" value={`${data.latitude.toFixed(5)}, ${data.longitude.toFixed(5)}`} />
        ) : null}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, gap: 8 },
  title: { fontSize: 22, fontFamily: 'Inter_700Bold' },
  subtitle: { fontSize: 14, marginBottom: 8, fontFamily: 'Inter_400Regular' },
  card: { borderRadius: 12, borderWidth: 1, padding: 14, gap: 10 },
  metaRow: { flexDirection: 'row', justifyContent: 'space-between', gap: 12 },
  metaLabel: { fontSize: 13, fontFamily: 'Inter_400Regular' },
  metaValue: { fontSize: 13, flexShrink: 1, textAlign: 'right', fontFamily: 'Inter_600SemiBold' },
});
