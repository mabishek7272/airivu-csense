import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Platform, StyleSheet, Switch, Text, View } from 'react-native';
import * as Notifications from 'expo-notifications';
import { registerPushToken } from '../../../src/api/tenant';
import { getDeviceId } from '../../../src/auth/device';
import { useAlertsSocket, type AlertEvent } from '../../../src/hooks/useAlertsSocket';
import { useColors } from '../../../src/theme/colors';
import { errorMessage } from '../../../src/utils/errorMessage';

const BANNER_DISMISS_MS = 6000;

/**
 * Push registration has no existing "run once after login" hook to attach to -
 * AuthContext.tsx's only `useEffect` is the one-time cold-start `tokenStore.isLoggedIn()`
 * check, and nothing else fires once per successful sign-in. Per this task's own
 * explicit fallback, that means push permission isn't requested (and no token is
 * registered) until the user visits this screen at least once. That's a real, accepted
 * v1 scope gap, not an oversight - it's called out here and in this task's own report so
 * it can be revisited (most likely by adding a real post-login hook to AuthContext) once
 * there's an actual reason to request push earlier than "the user opened Settings ->
 * Notifications".
 */
function usePushRegistration() {
  const [pushError, setPushError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function registerForPush() {
      try {
        const { status } = await Notifications.requestPermissionsAsync();
        if (status !== 'granted') return;

        if (Platform.OS === 'android') {
          // Required for a delivered push to actually surface as a heads-up
          // notification on Android 8+ - a channel-less notification is silently
          // downgraded to no visible alert at all.
          await Notifications.setNotificationChannelAsync('default', {
            name: 'default',
            importance: Notifications.AndroidImportance.DEFAULT,
          });
        }

        // getExpoPushTokenAsync() resolves its `projectId` from
        // Constants.expoConfig.extra.eas.projectId when no `projectId` option is
        // passed explicitly. This app's app.config.ts has no `extra.eas.projectId` set
        // yet (no EAS project has been created for mobile/app) - so this call is
        // expected to throw in that state. That failure is caught below and surfaced
        // as a real, named gap (not swallowed silently) rather than crashing the
        // screen; the fix, once there's a real EAS project id, is to add
        // `extra: { eas: { projectId: '<id>' } }` to app.config.ts.
        const { data: token } = await Notifications.getExpoPushTokenAsync();
        if (cancelled) return;

        const deviceId = await getDeviceId();
        await registerPushToken(deviceId, token);
      } catch (err) {
        if (!cancelled) setPushError(errorMessage(err));
      }
    }

    void registerForPush();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return pushError;
}

export default function NotificationsScreen() {
  const colors = useColors();
  const pushError = usePushRegistration();
  const [socketEnabled, setSocketEnabled] = useState(false);
  const [banner, setBanner] = useState<AlertEvent | null>(null);
  const bannerTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const onAlertEvent = useCallback((event: AlertEvent) => {
    setBanner(event);
    if (bannerTimerRef.current) clearTimeout(bannerTimerRef.current);
    bannerTimerRef.current = setTimeout(() => setBanner(null), BANNER_DISMISS_MS);
  }, []);

  useEffect(() => {
    return () => {
      if (bannerTimerRef.current) clearTimeout(bannerTimerRef.current);
    };
  }, []);

  // Not connected at all while the toggle is off - useAlertsSocket's own `enabled` gate
  // closes (or never opens) the socket rather than just ignoring its events, so a
  // disabled toggle burns no connection.
  const socketStatus = useAlertsSocket(socketEnabled, onAlertEvent);

  return (
    <View style={[styles.container, { backgroundColor: colors.surface }]}>
      {banner ? (
        <View style={[styles.banner, { backgroundColor: colors.accentSubtle, borderColor: colors.accent }]}>
          <Text style={{ color: colors.text, fontWeight: '600' }}>New alert</Text>
          <Text style={{ color: colors.textMuted, fontSize: 12 }} numberOfLines={1}>
            Incident {banner.incident_id} - {banner.type}
          </Text>
        </View>
      ) : null}

      <Text style={[styles.sectionTitle, { color: colors.text }]}>In-app alerts</Text>
      <View style={[styles.card, { borderColor: colors.border }]}>
        <View style={styles.rowBetween}>
          <View style={styles.rowText}>
            <Text style={{ color: colors.text }}>Notify me about new alerts while the app is open</Text>
            {socketEnabled ? (
              <Text style={{ color: colors.textMuted, fontSize: 12, marginTop: 2 }}>
                {socketStatus === 'open' ? 'Connected' : socketStatus === 'connecting' ? 'Connecting…' : 'Reconnecting…'}
              </Text>
            ) : null}
          </View>
          <Switch
            value={socketEnabled}
            onValueChange={setSocketEnabled}
            trackColor={{ false: colors.border, true: colors.accent }}
            testID="alerts-socket-toggle"
          />
        </View>
        <Text style={[styles.hint, { color: colors.textMuted }]}>
          This is separate from push notifications below - it only shows a banner while this screen is open, and
          drops the connection entirely when turned off.
        </Text>
      </View>

      <Text style={[styles.sectionTitle, { color: colors.text }]}>Push notifications</Text>
      <View style={[styles.card, { borderColor: colors.border }]}>
        <Text style={{ color: colors.text }}>
          This device registers for push notifications automatically when you visit this screen.
        </Text>
        {pushError ? (
          <Text style={{ color: colors.critical, marginTop: 6 }} accessibilityRole="alert">
            {pushError}
          </Text>
        ) : null}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, gap: 16, padding: 16 },
  banner: { borderRadius: 10, borderWidth: 1, gap: 2, padding: 12 },
  sectionTitle: { fontSize: 15, fontWeight: '600' },
  card: { borderRadius: 10, borderWidth: 1, gap: 8, padding: 14 },
  rowBetween: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between', gap: 12 },
  rowText: { flex: 1 },
  hint: { fontSize: 12 },
});
