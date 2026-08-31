import React, { useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { Link } from 'expo-router';
import { useIncidents } from '../../../src/hooks/useIncidents';
import { SeverityBadge, StatusBadge } from '../../../src/components/Badges';
import { LoadingState, ErrorState, EmptyState } from '../../../src/components/States';
import { useColors } from '../../../src/theme/colors';
import type { IncidentStatusFilter, IncidentSummary } from '../../../src/api/types';

const FILTERS: { label: string; value: IncidentStatusFilter }[] = [
  { label: 'Active', value: 'active' },
  { label: 'Resolved', value: 'resolved' },
  { label: 'Dismissed', value: 'dismissed' },
];

function FilterTabs({ value, onChange }: { value: IncidentStatusFilter; onChange: (v: IncidentStatusFilter) => void }) {
  const colors = useColors();
  return (
    <View style={[styles.filterRow, { borderBottomColor: colors.border }]}>
      {FILTERS.map((filter) => {
        const active = filter.value === value;
        return (
          <Pressable
            key={filter.label}
            onPress={() => onChange(filter.value)}
            style={[styles.filterTab, active && { borderBottomColor: colors.accent, borderBottomWidth: 2 }]}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
          >
            <Text style={{ color: active ? colors.accent : colors.textMuted, fontWeight: active ? '600' : '400' }}>
              {filter.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

function IncidentRow({ item }: { item: IncidentSummary }) {
  const colors = useColors();
  return (
    <Link href={`/(tabs)/incidents/${item.id}`} asChild>
      <Pressable style={[styles.row, { borderBottomColor: colors.border }]} accessibilityRole="button">
        <View style={styles.rowHeader}>
          <Text style={[styles.rowTitle, { color: colors.text }]} numberOfLines={1}>
            #{item.incident_number} {item.title}
          </Text>
        </View>
        <View style={styles.badgeRow}>
          <SeverityBadge severity={item.severity} />
          <StatusBadge status={item.status} />
        </View>
        <Text style={[styles.rowMeta, { color: colors.textMuted }]}>
          {item.detection_count} detection{item.detection_count === 1 ? '' : 's'} · last seen{' '}
          {new Date(item.last_detected_at).toLocaleString()}
        </Text>
      </Pressable>
    </Link>
  );
}

export default function IncidentsListScreen() {
  const colors = useColors();
  const [status, setStatus] = useState<IncidentStatusFilter>('active');
  const { items, loading, loadingMore, error, refresh, loadMore } = useIncidents(status);

  return (
    <View style={[styles.container, { backgroundColor: colors.surface }]}>
      <FilterTabs value={status} onChange={setStatus} />
      {loading && items.length === 0 ? (
        <LoadingState label="Loading incidents…" />
      ) : error && items.length === 0 ? (
        <ErrorState message={error} onRetry={refresh} />
      ) : items.length === 0 ? (
        <EmptyState message={`No ${status ?? ''} incidents.`} />
      ) : (
        <FlatList
          data={items}
          keyExtractor={(item) => item.id}
          renderItem={({ item }) => <IncidentRow item={item} />}
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
  filterRow: { flexDirection: 'row', borderBottomWidth: 1 },
  filterTab: { paddingHorizontal: 16, paddingVertical: 12 },
  row: { borderBottomWidth: 1, padding: 16, gap: 8 },
  rowHeader: { flexDirection: 'row' },
  rowTitle: { fontSize: 15, fontWeight: '600', flexShrink: 1 },
  badgeRow: { flexDirection: 'row', gap: 8 },
  rowMeta: { fontSize: 12 },
});
