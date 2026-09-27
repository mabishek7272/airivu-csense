import React from 'react';
import { FlatList, Image, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { Link } from 'expo-router';
import { useDetections } from '../../../../src/hooks/useDetections';
import { LoadingState, ErrorState, EmptyState } from '../../../../src/components/States';
import { useColors } from '../../../../src/theme/colors';
import type { DetectionOut } from '../../../../src/api/types';

/** Prefer the "annotated" evidence variant, fall back to "masked" if no annotated one
 * exists, and to nothing (not a broken-image placeholder) if `evidence` is empty or
 * every `url` is null - list/detail never return "original" for a mobile user anyway
 * (backend/tenant_api/app/api/detections.py's own `evidence.download` gate). */
function previewUrl(detection: DetectionOut): string | null {
  const annotated = detection.evidence.find((e) => e.variant === 'annotated' && e.url);
  if (annotated?.url) return annotated.url;
  const masked = detection.evidence.find((e) => e.variant === 'masked' && e.url);
  return masked?.url ?? null;
}

function DetectionRow({ item }: { item: DetectionOut }) {
  const colors = useColors();
  const image = previewUrl(item);
  return (
    <Link href={`/(tabs)/home/detections/${item.detection_id}`} asChild>
      <Pressable style={[styles.row, { borderBottomColor: colors.border }]} accessibilityRole="button">
        {image ? (
          <Image source={{ uri: image }} style={[styles.thumb, { backgroundColor: colors.surfaceSunken }]} />
        ) : (
          <View style={[styles.thumb, styles.thumbPlaceholder, { backgroundColor: colors.surfaceSunken }]} />
        )}
        <View style={styles.rowBody}>
          <Text style={[styles.rowTitle, { color: colors.text }]} numberOfLines={1}>
            {item.camera.camera_name} · {item.location.site_name}
          </Text>
          <Text style={[styles.rowMeta, { color: colors.textMuted }]}>
            {Math.round(item.confidence * 100)}% confidence · {new Date(item.captured_at).toLocaleString()}
          </Text>
        </View>
      </Pressable>
    </Link>
  );
}

export default function DetectionsListScreen() {
  const colors = useColors();
  const { items, loading, loadingMore, error, refresh, loadMore } = useDetections();

  return (
    <View style={[styles.container, { backgroundColor: colors.surface }]}>
      {loading && items.length === 0 ? (
        <LoadingState label="Loading detections…" />
      ) : error && items.length === 0 ? (
        <ErrorState message={error} onRetry={refresh} />
      ) : items.length === 0 ? (
        <EmptyState message="No detections." />
      ) : (
        <FlatList
          data={items}
          keyExtractor={(item) => item.detection_id}
          renderItem={({ item }) => <DetectionRow item={item} />}
          refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.accent} />}
          onEndReachedThreshold={0.4}
          onEndReached={loadMore}
          ListFooterComponent={loadingMore ? <LoadingState label="Loading more…" /> : null}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  row: { borderBottomWidth: 1, flexDirection: 'row', gap: 12, padding: 16 },
  thumb: { borderRadius: 8, height: 64, width: 64 },
  thumbPlaceholder: {},
  rowBody: { flex: 1, gap: 4, justifyContent: 'center' },
  rowTitle: { fontSize: 15, fontWeight: '600' },
  rowMeta: { fontSize: 12 },
});
