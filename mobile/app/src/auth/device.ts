import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';

/**
 * The app's own install id, sent at sign-in so the plan's "N signed-in devices" limit
 * counts phones rather than logins: signing in again on the same phone reuses its slot.
 * Generated once and kept in SecureStore (Keychain on iOS survives a reinstall, so a
 * reinstall does not burn a second slot there; on Android it does, and the account
 * owner can free the old one under Devices).
 */
const KEY_DEVICE_ID = 'csense_device_id';

function randomId(): string {
  const bytes = Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));
  return bytes.map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function getDeviceId(): Promise<string> {
  const existing = await SecureStore.getItemAsync(KEY_DEVICE_ID);
  if (existing) return existing;
  const created = randomId();
  await SecureStore.setItemAsync(KEY_DEVICE_ID, created);
  return created;
}

/** A label the account owner can recognise in the device list. */
export function deviceName(): string {
  if (Platform.OS === 'android') {
    const { Brand, Model } = Platform.constants;
    return [Brand, Model].filter(Boolean).join(' ') || 'Android device';
  }
  return Platform.OS === 'ios' && Platform.isPad ? 'iPad' : 'iPhone';
}
