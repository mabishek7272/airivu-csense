import { MaterialCommunityIcons } from '@expo/vector-icons';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, FlatList, Image, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Btn, Kicker, Tag } from '../components/ui';
import { clearHistory, loadHistory, type HistoryEntry, type HistorySource, type Severity } from '../history';
import { color, fontHeading, space, textMuted } from '../theme';

const SEVERITY_TAG: Record<Severity, 'outline' | 'accent' | 'neutral'> = {
  low: 'neutral',
  medium: 'outline',
  high: 'accent',
};

type SeverityFilter = 'all' | Severity;
type SourceFilter = 'all' | HistorySource;

function Icon({ name, size = 22, color: c = color.neutral500 }: { name: string; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name as never} size={size} color={c} />;
}

function FilterChip({ label, active, onPress }: { label: string; active: boolean; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="radio" accessibilityState={{ selected: active }}>
      <Tag variant={active ? 'accent' : 'neutral'}>{label}</Tag>
    </Pressable>
  );
}

export function HistoryScreen() {
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter>('all');
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all');
  const [detail, setDetail] = useState<HistoryEntry | null>(null);

  const refresh = useCallback(() => {
    setLoadError(false);
    loadHistory()
      .then(setEntries)
      .catch(() => setLoadError(true));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    if (!entries) return [];
    return entries.filter(
      (e) => (severityFilter === 'all' || e.severity === severityFilter) && (sourceFilter === 'all' || e.source === sourceFilter),
    );
  }, [entries, severityFilter, sourceFilter]);

  function onClear() {
    Alert.alert('Clear history?', 'This removes every entry and thumbnail on this device.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Clear',
        style: 'destructive',
        onPress: () =>
          clearHistory()
            .then(refresh)
            .catch(() => Alert.alert('Could not clear history', 'Something went wrong. Please try again.')),
      },
    ]);
  }

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Kicker>Local to this device</Kicker>
        <Text style={styles.title}>History</Text>
      </View>
      <Text style={styles.note}>
        Nothing here is uploaded. Detections stay on this phone and are cleared when you tap Clear.
      </Text>

      {loadError ? (
        <View style={styles.empty}>
          <Icon name="alert-circle-outline" size={32} />
          <Text style={{ color: textMuted, fontSize: 14, marginTop: 8, textAlign: 'center' }}>
            Couldn't load history.
          </Text>
          <Btn variant="secondary" onPress={refresh} style={{ marginTop: 12 }}>
            Retry
          </Btn>
        </View>
      ) : entries === null ? null : entries.length === 0 ? (
        <View style={styles.empty}>
          <Icon name="history" size={32} />
          <Text style={{ color: textMuted, fontSize: 14, marginTop: 8, textAlign: 'center' }}>
            No detections yet. Try a model or run Live Monitoring to see results here.
          </Text>
        </View>
      ) : (
        <>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.filterRow}>
            <FilterChip label="All types" active={sourceFilter === 'all'} onPress={() => setSourceFilter('all')} />
            <FilterChip label="Try a Model" active={sourceFilter === 'detect'} onPress={() => setSourceFilter('detect')} />
            <FilterChip label="Live Monitoring" active={sourceFilter === 'monitor'} onPress={() => setSourceFilter('monitor')} />
          </ScrollView>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.filterRow}>
            <FilterChip label="All severities" active={severityFilter === 'all'} onPress={() => setSeverityFilter('all')} />
            <FilterChip label="Low" active={severityFilter === 'low'} onPress={() => setSeverityFilter('low')} />
            <FilterChip label="Medium" active={severityFilter === 'medium'} onPress={() => setSeverityFilter('medium')} />
            <FilterChip label="High" active={severityFilter === 'high'} onPress={() => setSeverityFilter('high')} />
          </ScrollView>

          <Btn variant="secondary" onPress={onClear}>
            Clear all
          </Btn>

          {filtered.length === 0 ? (
            <Text style={{ color: textMuted, fontSize: 13, marginTop: 12 }}>No entries match these filters.</Text>
          ) : (
            <FlatList
              data={filtered}
              keyExtractor={(e) => e.id}
              style={{ flex: 1, marginTop: space[2] }}
              renderItem={({ item }) => (
                <Pressable
                  onPress={() => setDetail(item)}
                  style={styles.row}
                  accessibilityRole="button"
                  accessibilityLabel={`${item.label}, ${item.severity} severity, opens details`}
                >
                  {item.thumbnailUri ? (
                    <Image source={{ uri: item.thumbnailUri }} style={styles.thumb} />
                  ) : (
                    <View style={[styles.thumb, styles.thumbPlaceholder]} />
                  )}
                  <View style={{ flex: 1 }}>
                    <Text style={{ color: color.text, fontSize: 14 }}>{item.label}</Text>
                    <Text style={{ color: textMuted, fontSize: 12 }}>
                      {new Date(item.timestamp).toLocaleTimeString()} · {item.source === 'monitor' ? 'Live Monitoring' : 'Try a Model'}
                    </Text>
                  </View>
                  <Tag variant={SEVERITY_TAG[item.severity]}>{item.severity.toUpperCase()}</Tag>
                </Pressable>
              )}
            />
          )}
        </>
      )}

      <Modal visible={detail !== null} transparent animationType="fade" onRequestClose={() => setDetail(null)}>
        <Pressable style={styles.modalBackdrop} onPress={() => setDetail(null)}>
          {detail && (
            <Pressable style={styles.modalCard} onPress={() => {}}>
              {detail.thumbnailUri ? (
                <Image source={{ uri: detail.thumbnailUri }} style={styles.modalThumb} />
              ) : (
                <View style={[styles.modalThumb, styles.thumbPlaceholder]} />
              )}
              <Text style={{ color: color.text, fontSize: 17, fontFamily: fontHeading, marginTop: 12 }}>{detail.label}</Text>
              <Text style={{ color: textMuted, fontSize: 13, marginTop: 4 }}>
                {detail.confidence !== null ? `${Math.round(detail.confidence * 100)}% confidence · ` : ''}
                {new Date(detail.timestamp).toLocaleString()}
              </Text>
              <Text style={{ color: textMuted, fontSize: 13 }}>
                {detail.source === 'monitor' ? 'Live Monitoring' : 'Try a Model'} · {detail.severity} severity
              </Text>
              <Btn variant="secondary" onPress={() => setDetail(null)} style={{ marginTop: 14 }}>
                Close
              </Btn>
            </Pressable>
          )}
        </Pressable>
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: color.bg, padding: 16, gap: space[3] },
  header: { gap: 2 },
  title: { fontSize: 24, fontFamily: fontHeading, color: color.text },
  note: { color: textMuted, fontSize: 12 },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 32 },
  filterRow: { gap: 6, paddingVertical: 2 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingVertical: 10 },
  thumb: { width: 48, height: 48, borderRadius: 8, backgroundColor: color.neutral200 },
  thumbPlaceholder: { alignItems: 'center', justifyContent: 'center' },
  modalBackdrop: { flex: 1, backgroundColor: 'rgba(32,34,44,0.5)', alignItems: 'center', justifyContent: 'center', padding: 24 },
  modalCard: { backgroundColor: color.surface, borderRadius: 14, padding: 18, width: '100%', maxWidth: 340 },
  modalThumb: { width: '100%', height: 180, borderRadius: 10, backgroundColor: color.neutral200 },
});
