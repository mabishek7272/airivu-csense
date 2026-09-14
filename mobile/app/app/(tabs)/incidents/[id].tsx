import React, { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useLocalSearchParams } from 'expo-router';
import { useIncidentDetail } from '../../../src/hooks/useIncidentDetail';
import { SeverityBadge, StatusBadge } from '../../../src/components/Badges';
import { LoadingState, ErrorState } from '../../../src/components/States';
import { useColors } from '../../../src/theme/colors';

/** Mirrors frontend/customer-crm/src/pages/IncidentDetailPage.tsx's own action map -
 * only acknowledge/investigate/resolve/dismiss are real endpoints
 * (backend/tenant_api/app/api/incidents.py has no `/escalate` route, so "escalated" is
 * reached some other way, never offered here as a button). */
const NEXT_ACTIONS: Record<string, { key: 'acknowledge' | 'investigate' | 'resolve' | 'dismiss'; label: string; closing: boolean }[]> = {
  open: [
    { key: 'acknowledge', label: 'Acknowledge', closing: false },
    { key: 'investigate', label: 'Start investigating', closing: false },
    { key: 'resolve', label: 'Resolve', closing: true },
    { key: 'dismiss', label: 'Dismiss', closing: true },
  ],
  acknowledged: [
    { key: 'investigate', label: 'Start investigating', closing: false },
    { key: 'resolve', label: 'Resolve', closing: true },
    { key: 'dismiss', label: 'Dismiss', closing: true },
  ],
  investigating: [
    { key: 'resolve', label: 'Resolve', closing: true },
    { key: 'dismiss', label: 'Dismiss', closing: true },
  ],
  escalated: [
    { key: 'investigate', label: 'Start investigating', closing: false },
    { key: 'resolve', label: 'Resolve', closing: true },
    { key: 'dismiss', label: 'Dismiss', closing: true },
  ],
  resolved: [],
  dismissed: [],
};

/** Same real, stable resolution-code vocabulary as the web CRM's own
 * IncidentDetailPage.tsx `RESOLUTION_CODES` - free text server-side, but this is the set
 * both apps actually offer so a resolved/dismissed incident means the same thing
 * regardless of which client closed it. */
const RESOLUTION_CODES = [
  { value: 'confirmed_true_positive', label: 'Confirmed — action taken' },
  { value: 'false_positive', label: 'False positive' },
  { value: 'duplicate', label: 'Duplicate of another incident' },
  { value: 'no_action_required', label: 'Genuine, no action required' },
];

export default function IncidentDetailScreen() {
  const params = useLocalSearchParams<{ id: string | string[] }>();
  const id = Array.isArray(params.id) ? params.id[0] : params.id;
  const colors = useColors();
  const { data, loading, error, transitioning, transitionError, refresh, acknowledge, investigate, resolve, dismiss } =
    useIncidentDetail(id);
  const [resolutionCode, setResolutionCode] = useState(RESOLUTION_CODES[0].value);

  if (loading && !data) return <LoadingState label="Loading incident…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  const actions = NEXT_ACTIONS[data.status] ?? [];

  async function runAction(actionKey: 'acknowledge' | 'investigate' | 'resolve' | 'dismiss') {
    try {
      if (actionKey === 'acknowledge') await acknowledge();
      else if (actionKey === 'investigate') await investigate();
      else if (actionKey === 'resolve') await resolve(resolutionCode);
      else await dismiss(resolutionCode);
    } catch {
      // transitionError already carries the real server message, rendered below - a 409
      // (someone else moved it first) already triggered a re-fetch inside the hook.
    }
  }

  return (
    <ScrollView style={{ backgroundColor: colors.surface }} contentContainerStyle={styles.content}>
      <Text style={[styles.title, { color: colors.text }]}>
        #{data.incident_number} {data.title}
      </Text>
      <View style={styles.badgeRow}>
        <SeverityBadge severity={data.severity} />
        <StatusBadge status={data.status} />
      </View>
      {data.summary ? <Text style={[styles.summary, { color: colors.text }]}>{data.summary}</Text> : null}

      <View style={[styles.metaBlock, { borderColor: colors.border }]}>
        <Text style={[styles.metaLine, { color: colors.textMuted }]}>Type: {data.type_code}</Text>
        <Text style={[styles.metaLine, { color: colors.textMuted }]}>Detections: {data.detection_count}</Text>
        <Text style={[styles.metaLine, { color: colors.textMuted }]}>
          First detected: {new Date(data.first_detected_at).toLocaleString()}
        </Text>
        <Text style={[styles.metaLine, { color: colors.textMuted }]}>
          Last detected: {new Date(data.last_detected_at).toLocaleString()}
        </Text>
        {data.resolution_code ? (
          <Text style={[styles.metaLine, { color: colors.textMuted }]}>Resolution: {data.resolution_code}</Text>
        ) : null}
      </View>

      {actions.length > 0 ? (
        <View style={styles.actionsBlock}>
          <Text style={[styles.sectionTitle, { color: colors.text }]}>Actions</Text>

          {actions.some((a) => a.closing) ? (
            <View style={styles.resolutionRow}>
              {RESOLUTION_CODES.map((code) => {
                const active = code.value === resolutionCode;
                return (
                  <Pressable
                    key={code.value}
                    onPress={() => setResolutionCode(code.value)}
                    style={[
                      styles.chip,
                      { borderColor: active ? colors.accent : colors.border, backgroundColor: active ? colors.accentSubtle : 'transparent' },
                    ]}
                  >
                    <Text style={{ color: active ? colors.accent : colors.textMuted, fontSize: 12 }}>{code.label}</Text>
                  </Pressable>
                );
              })}
            </View>
          ) : null}

          {transitionError ? <Text style={[styles.error, { color: colors.critical }]}>{transitionError}</Text> : null}

          <View style={styles.actionRow}>
            {actions.map((action) => (
              <Pressable
                key={action.key}
                onPress={() => runAction(action.key)}
                disabled={transitioning}
                // Crimson, not colors.accent (rose) - same "crimson is the fill colour for
                // a primary action" rule as login.tsx's submit button and the web CRM's
                // button.primary (System.dc.html's own usage note).
                style={[styles.actionButton, { backgroundColor: colors.crimson, opacity: transitioning ? 0.6 : 1 }]}
                accessibilityRole="button"
              >
                <Text style={[styles.actionButtonText, { color: colors.textInverse }]}>{action.label}</Text>
              </Pressable>
            ))}
          </View>
        </View>
      ) : null}

      <View style={styles.timelineBlock}>
        <Text style={[styles.sectionTitle, { color: colors.text }]}>Timeline</Text>
        {data.events.map((event, index) => (
          <View key={index} style={[styles.timelineRow, { borderColor: colors.border }]}>
            <Text style={[styles.timelineType, { color: colors.text }]}>{event.event_type}</Text>
            <Text style={[styles.metaLine, { color: colors.textMuted }]}>
              {new Date(event.occurred_at).toLocaleString()}
              {event.previous_status && event.new_status ? ` · ${event.previous_status} → ${event.new_status}` : ''}
            </Text>
          </View>
        ))}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, gap: 16 },
  title: { fontSize: 20, fontWeight: '700' },
  badgeRow: { flexDirection: 'row', gap: 8 },
  summary: { fontSize: 15, lineHeight: 21 },
  metaBlock: { borderRadius: 10, borderWidth: 1, gap: 4, padding: 12 },
  metaLine: { fontSize: 13 },
  sectionTitle: { fontSize: 15, fontWeight: '600', marginBottom: 8 },
  actionsBlock: { gap: 8 },
  resolutionRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: { borderRadius: 16, borderWidth: 1, paddingHorizontal: 12, paddingVertical: 6 },
  actionRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  actionButton: { borderRadius: 8, paddingHorizontal: 14, paddingVertical: 10 },
  actionButtonText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  error: { fontSize: 13 },
  timelineBlock: { gap: 8 },
  timelineRow: { borderLeftWidth: 2, gap: 2, paddingLeft: 10, paddingVertical: 4 },
  timelineType: { fontSize: 13, fontWeight: '600' },
});
