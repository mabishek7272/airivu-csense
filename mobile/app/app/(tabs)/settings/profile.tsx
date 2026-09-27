import React, { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { confirmTotp, enrollTotp } from '../../../src/api/tenant';
import { ErrorState, LoadingState } from '../../../src/components/States';
import { useColors } from '../../../src/theme/colors';
import { useProfile } from '../../../src/hooks/useProfile';
import { errorMessage } from '../../../src/utils/errorMessage';

/** Ported from the web CRM's own `EnrollMfaDialog` (frontend/customer-crm/src/pages/
 * SettingsPage.tsx) against the identical backend contract - same 3-phase state machine
 * ("loading" | "scan" | "codes"), starting `enrollTotp()` automatically on mount. Rendered
 * inline (expanding below the "Enroll TOTP" button) rather than as a popup - this app has
 * no `<Dialog>`-equivalent component the way the web CRM does. No QR code in v1: `secret`/
 * `otpauth_uri` are shown as manual-entry text, matching the web version's own "or use
 * this link" fallback (which is the *primary* path here, since there's no QR image
 * either). */
function EnrollTotpSection({ onCancel, onEnrolled }: { onCancel: () => void; onEnrolled: () => void }) {
  const colors = useColors();
  const [phase, setPhase] = useState<'loading' | 'scan' | 'codes'>('loading');
  const [secret, setSecret] = useState('');
  const [otpauthUri, setOtpauthUri] = useState('');
  const [code, setCode] = useState('');
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    enrollTotp()
      .then((res) => {
        if (cancelled) return;
        setSecret(res.secret);
        setOtpauthUri(res.otpauth_uri);
        setPhase('scan');
      })
      .catch((err) => {
        if (cancelled) return;
        setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onConfirm = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await confirmTotp(code);
      setRecoveryCodes(res.recovery_codes);
      setPhase('codes');
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <View style={[styles.card, { borderColor: colors.border }]}>
      {phase === 'loading' && !error && (
        <View style={styles.loadingRow}>
          <ActivityIndicator color={colors.accent} />
          <Text style={{ color: colors.textMuted }}>Setting up…</Text>
        </View>
      )}

      {phase === 'loading' && error && (
        <View style={styles.sectionGap}>
          <Text style={{ color: colors.critical }}>{error}</Text>
          <Pressable onPress={onCancel} accessibilityRole="button">
            <Text style={{ color: colors.textMuted }}>Close</Text>
          </Pressable>
        </View>
      )}

      {phase === 'scan' && (
        <View style={styles.sectionGap}>
          <Text style={{ color: colors.text }}>
            Add this to an authenticator app (Google Authenticator, 1Password, Authy):
          </Text>
          <Text
            selectable
            style={[styles.mono, { color: colors.text, backgroundColor: colors.surfaceSunken, borderRadius: 8, padding: 10 }]}
          >
            {secret}
          </Text>
          <Text style={{ color: colors.textMuted, fontSize: 12 }}>
            Or use this link on a device with the app installed:
          </Text>
          <Text selectable style={[styles.mono, { color: colors.textMuted, fontSize: 11 }]}>
            {otpauthUri}
          </Text>

          <Text style={{ color: colors.text, marginTop: 4 }}>Enter the 6-digit code from your app</Text>
          <TextInput
            style={[styles.input, { borderColor: colors.border, color: colors.text }]}
            placeholder="000000"
            placeholderTextColor={colors.textMuted}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="number-pad"
            maxLength={6}
            value={code}
            onChangeText={(text) => setCode(text.replace(/\D/g, ''))}
            testID="mfa-enroll-code"
          />

          {submitting ? (
            <View style={styles.loadingRow}>
              <ActivityIndicator color={colors.accent} />
              <Text style={{ color: colors.textMuted }}>Confirming…</Text>
            </View>
          ) : error ? (
            <Text style={{ color: colors.critical }} accessibilityRole="alert">
              {error}
            </Text>
          ) : null}

          <View style={styles.actionsRow}>
            <Pressable onPress={onCancel} accessibilityRole="button">
              <Text style={{ color: colors.textMuted }}>Cancel</Text>
            </Pressable>
            <Pressable
              onPress={() => void onConfirm()}
              disabled={code.length !== 6 || submitting}
              style={[styles.button, { backgroundColor: colors.crimson, opacity: code.length !== 6 || submitting ? 0.6 : 1 }]}
              accessibilityRole="button"
              testID="mfa-enroll-confirm"
            >
              <Text style={[styles.buttonText, { color: colors.textInverse }]}>Confirm</Text>
            </Pressable>
          </View>
        </View>
      )}

      {phase === 'codes' && (
        <View style={styles.sectionGap}>
          <Text style={{ color: colors.text }}>
            <Text style={{ fontWeight: '700' }}>Save these recovery codes.</Text> Each can be used once if you lose
            access to your authenticator app. They will not be shown again.
          </Text>
          <View style={styles.codesGrid}>
            {recoveryCodes.map((rc) => (
              <Text key={rc} style={[styles.mono, { color: colors.text, width: '50%' }]}>
                {rc}
              </Text>
            ))}
          </View>
          <Pressable
            onPress={onEnrolled}
            style={[styles.button, { backgroundColor: colors.crimson, alignSelf: 'flex-start' }]}
            accessibilityRole="button"
            testID="mfa-enroll-ack"
          >
            <Text style={[styles.buttonText, { color: colors.textInverse }]}>I've saved these codes</Text>
          </Pressable>
        </View>
      )}
    </View>
  );
}

export default function ProfileScreen() {
  const colors = useColors();
  const { data, loading, error, refresh } = useProfile();
  const [enrolling, setEnrolling] = useState(false);

  return (
    <ScrollView style={{ backgroundColor: colors.surface }} contentContainerStyle={styles.content}>
      {loading ? (
        <LoadingState label="Loading profile…" />
      ) : error || !data ? (
        <ErrorState message={error ?? 'Could not load your profile.'} onRetry={refresh} />
      ) : (
        <>
          <Text style={[styles.sectionTitle, { color: colors.text }]}>Profile</Text>
          <View style={[styles.card, { borderColor: colors.border }]}>
            <Text style={[styles.name, { color: colors.text }]}>{data.profile.display_name}</Text>
            <Text style={{ color: colors.textMuted }}>{data.profile.email}</Text>
            <Text style={{ color: colors.textMuted, marginTop: 8 }}>{data.profile.organization_display_name}</Text>
          </View>

          <Text style={[styles.sectionTitle, { color: colors.text }]}>Two-factor authentication</Text>
          <View style={[styles.card, { borderColor: colors.border }]}>
            <View style={styles.rowBetween}>
              <View>
                <Text style={{ color: colors.text, fontWeight: '600' }}>
                  {data.mfa.enrolled ? 'Turned on' : 'Turned off'}
                </Text>
                {data.mfa.enrolled ? (
                  <Text style={{ color: colors.textMuted }}>
                    {data.mfa.recovery_codes_remaining} recovery code
                    {data.mfa.recovery_codes_remaining === 1 ? '' : 's'} remaining
                  </Text>
                ) : null}
              </View>
              {!data.mfa.enrolled && !enrolling ? (
                <Pressable
                  onPress={() => setEnrolling(true)}
                  style={[styles.button, { backgroundColor: colors.crimson }]}
                  accessibilityRole="button"
                  testID="mfa-enroll-start"
                >
                  <Text style={[styles.buttonText, { color: colors.textInverse }]}>Enroll TOTP</Text>
                </Pressable>
              ) : null}
            </View>

            {enrolling ? (
              <View style={styles.enrollSection}>
                <EnrollTotpSection
                  onCancel={() => setEnrolling(false)}
                  onEnrolled={() => {
                    setEnrolling(false);
                    refresh();
                  }}
                />
              </View>
            ) : null}
          </View>
        </>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, gap: 16 },
  sectionTitle: { fontSize: 15, fontWeight: '600' },
  card: { borderRadius: 10, borderWidth: 1, gap: 6, padding: 14 },
  name: { fontSize: 17, fontWeight: '700' },
  rowBetween: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between' },
  enrollSection: { marginTop: 12 },
  sectionGap: { gap: 10 },
  loadingRow: { alignItems: 'center', flexDirection: 'row', gap: 8 },
  mono: { fontFamily: 'monospace' },
  input: { borderRadius: 8, borderWidth: 1, fontSize: 16, paddingHorizontal: 12, paddingVertical: 10 },
  actionsRow: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between', marginTop: 4 },
  button: { borderRadius: 8, paddingHorizontal: 14, paddingVertical: 10 },
  buttonText: { fontWeight: '600' },
  codesGrid: { flexDirection: 'row', flexWrap: 'wrap', marginVertical: 4 },
});
