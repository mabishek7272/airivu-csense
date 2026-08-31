import { Platform } from 'react-native';

/**
 * `EXPO_PUBLIC_*` env vars are Expo's own real, standard mechanism for build-time-
 * inlined config (available since SDK 49) - the direct equivalent of the native
 * Android app's own `BuildConfig.API_BASE_URL`/`API_HOST_HEADER` fields
 * (`app/build.gradle.kts`'s `-PapiBaseUrl`/`-PapiHost`), re-expressed for Expo. Set
 * them in a real `.env` file (gitignored, see README) or inline before `expo start`.
 *
 * Defaults target this repo's own local dev stack. The default host differs by
 * platform on purpose, not by oversight: an Android emulator reaches the host machine
 * via the `10.0.2.2` alias (a virtualized network), while the iOS *Simulator* (not a
 * physical device) shares the host machine's own network stack directly and reaches it
 * as `localhost` - the same distinction the backend's own e2e scripts never had to make
 * (they always ran on the host itself). A physical device on either platform needs the
 * dev machine's real LAN IP instead of either default - see README.
 */
export const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_BASE_URL ??
  (Platform.OS === 'android' ? 'http://10.0.2.2:8080' : 'http://localhost:8080');

/** The `Host` header Traefik's own path/host-based routing needs
 * (infra/traefik/dynamic.yml) - the same header every backend e2e script passes
 * explicitly when it isn't talking to a real DNS-resolved domain. Against a real
 * production `https://` domain (docs/10_PRODUCTION_DEPLOYMENT_GUIDE.md), this is a
 * harmless no-op once set to that real domain. */
export const API_HOST_HEADER = process.env.EXPO_PUBLIC_API_HOST ?? 'app.localhost';
