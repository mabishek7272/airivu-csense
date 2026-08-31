import React, { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useAuth } from '../../src/auth/AuthContext';
import { useLicense } from '../../src/hooks/useLicense';
import { LoadingState, ErrorState } from '../../src/components/States';
import { useColors } from '../../src/theme/colors';

function QuotaRow({ code, limit, consumed }: { code: string; limit: number; consumed: number }) {
  const colors = useColors();
  const pct = limit > 0 ? Math.min(100, Math.round((consumed / limit) * 100)) : 0;
  return (
    <View style={styles.quotaRow}>
      <View style={styles.quotaLabelRow}>
        <Text style={[styles.quotaLabel, { color: colors.text }]}>{code}</Text>
        <Text style={[styles.quotaValue, { color: colors.textMuted }]}>
          {consumed} / {limit}
        </Text>
      </View>
      <View style={[styles.quotaBar, { backgroundColor: colors.surfaceSunken }]}>
        <View style={[styles.quotaBarFill, { backgroundColor: colors.accent, width: `${pct}%` }]} />
      </View>
    </View>
  );
}

export default function AccountScreen() {
  const colors = useColors();
  const { logout } = useAuth();
  const { data: license, loading, error, refresh } = useLicense();
  const [loggingOut, setLoggingOut] = useState(false);

  const onLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <ScrollView style={{ backgroundColor: colors.surface }} contentContainerStyle={styles.content}>
      <Text style={[styles.sectionTitle, { color: colors.text }]}>License</Text>
      {loading ? (
        <LoadingState label="Loading license…" />
      ) : error ? (
        <ErrorState message={error} onRetry={refresh} />
      ) : !license ? (
        <Text style={[styles.muted, { color: colors.textMuted }]}>No license assigned to this tenant yet.</Text>
      ) : (
        <View style={[styles.card, { borderColor: colors.border }]}>
          <Text style={[styles.planName, { color: colors.text }]}>{license.plan_name}</Text>
          <Text style={[styles.muted, { color: colors.textMuted, textTransform: 'capitalize' }]}>
            Status: {license.status}
          </Text>
          {license.expires_at ? (
            <Text style={[styles.muted, { color: colors.textMuted }]}>
              Expires: {new Date(license.expires_at).toLocaleDateString()}
            </Text>
          ) : null}
          {license.quota_usage.length > 0 ? (
            <View style={styles.quotaBlock}>
              {license.quota_usage.map((quota) => (
                <QuotaRow
                  key={quota.quota_code}
                  code={quota.quota_code}
                  limit={quota.limit_value}
                  consumed={quota.consumed_value}
                />
              ))}
            </View>
          ) : null}
        </View>
      )}

      <Pressable
        onPress={onLogout}
        disabled={loggingOut}
        style={[styles.logoutButton, { borderColor: colors.critical, opacity: loggingOut ? 0.6 : 1 }]}
        accessibilityRole="button"
      >
        <Text style={{ color: colors.critical, fontWeight: '600' }}>{loggingOut ? 'Signing out…' : 'Sign out'}</Text>
      </Pressable>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, gap: 16 },
  sectionTitle: { fontSize: 15, fontWeight: '600' },
  muted: { fontSize: 13 },
  card: { borderRadius: 10, borderWidth: 1, gap: 6, padding: 14 },
  planName: { fontSize: 17, fontWeight: '700' },
  quotaBlock: { gap: 10, marginTop: 8 },
  quotaRow: { gap: 4 },
  quotaLabelRow: { flexDirection: 'row', justifyContent: 'space-between' },
  quotaLabel: { fontSize: 12, fontWeight: '600' },
  quotaValue: { fontSize: 12 },
  quotaBar: { borderRadius: 3, height: 6, overflow: 'hidden' },
  quotaBarFill: { height: '100%' },
  logoutButton: { alignItems: 'center', borderRadius: 8, borderWidth: 1, marginTop: 8, paddingVertical: 12 },
});
