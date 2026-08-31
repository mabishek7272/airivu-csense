import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { useColors, type Palette } from '../theme/colors';

/**
 * Mirrors frontend/customer-crm/src/components/Badges.tsx's own severity/status colour
 * mapping exactly (same tokens, see src/theme/colors.ts) - and the same "label is always
 * text, never colour alone" rule, for the same colour-blind-operator reasoning that
 * component's own comment states.
 */

type Severity = 'info' | 'low' | 'medium' | 'high' | 'critical';
type IncidentStatus = 'open' | 'acknowledged' | 'investigating' | 'escalated' | 'resolved' | 'dismissed';

function tone(colors: Palette, key: Severity | 'neutral') {
  switch (key) {
    case 'critical':
      return { bg: colors.criticalSubtle, fg: colors.critical };
    case 'high':
      return { bg: colors.highSubtle, fg: colors.high };
    case 'medium':
      return { bg: colors.mediumSubtle, fg: colors.medium };
    case 'low':
      return { bg: colors.lowSubtle, fg: colors.low };
    case 'info':
      return { bg: colors.infoSubtle, fg: colors.info };
    default:
      return { bg: colors.surfaceSunken, fg: colors.textMuted };
  }
}

/** `accessibilityLabel` carries the "Severity: "/"Status: " prefix for a screen reader,
 * same information the web Badges.tsx puts in a visually-hidden span - the visible text
 * itself stays just the value, which is all there's room for in a compact badge. */
function Badge({ label, prefix, bg, fg }: { label: string; prefix: string; bg: string; fg: string }) {
  return (
    <View
      style={[styles.badge, { backgroundColor: bg, borderColor: fg }]}
      accessible
      accessibilityLabel={`${prefix}${label}`}
    >
      <Text style={[styles.text, { color: fg }]}>{label}</Text>
    </View>
  );
}

export function SeverityBadge({ severity }: { severity: string }) {
  const colors = useColors();
  const key = (['info', 'low', 'medium', 'high', 'critical'] as const).includes(severity as Severity)
    ? (severity as Severity)
    : 'neutral';
  const { bg, fg } = tone(colors, key);
  return <Badge label={severity} prefix="Severity: " bg={bg} fg={fg} />;
}

const STATUS_TONE: Record<IncidentStatus, Severity> = {
  open: 'critical',
  escalated: 'critical',
  investigating: 'high',
  acknowledged: 'medium',
  resolved: 'low',
  dismissed: 'info',
};

export function StatusBadge({ status }: { status: string }) {
  const colors = useColors();
  const key = STATUS_TONE[status as IncidentStatus] ?? 'neutral';
  const { bg, fg } = tone(colors, key);
  return <Badge label={status} prefix="Status: " bg={bg} fg={fg} />;
}

const styles = StyleSheet.create({
  badge: {
    alignSelf: 'flex-start',
    borderRadius: 6,
    borderWidth: 1,
    paddingHorizontal: 8,
    paddingVertical: 3,
  },
  text: {
    fontSize: 12,
    fontWeight: '600',
    textTransform: 'capitalize',
  },
});
