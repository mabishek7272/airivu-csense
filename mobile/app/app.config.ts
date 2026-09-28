import type { ExpoConfig } from 'expo/config';

const VARIANT = process.env.APP_VARIANT ?? 'csense';

const BRAND_NAMES: Record<string, string> = {
  apti: 'Apti', eaigleye: 'eAIgleye', '3rdi': '3RDi', csense: 'CSense',
};

const BRAND_IDS: Record<string, string> = {
  apti: 'ai.airivu.apti.client', eaigleye: 'ai.airivu.eaigleye', '3rdi': 'ai.airivu.threerdi', csense: 'ai.airivu.csense',
};

// Native splash/adaptive-icon background per brand - must match that brand's own
// in-app canvas colour (src/theme/colors.ts's BRAND_PALETTE), or the OS-level splash
// visibly flashes the wrong colour before the JS UI mounts. CSense moved to its own
// light "Majestic White & Cherry Purple" canvas (#f8f7fb) on 2026-09-28, replacing the
// old dark navy that matched its now-retired dark theme.
const BRAND_BACKGROUND: Record<string, string> = {
  apti: '#f3f5fe', eaigleye: '#0F2A43', '3rdi': '#0F2A43', csense: '#f8f7fb',
};

export default (): ExpoConfig => ({
  name: BRAND_NAMES[VARIANT] ?? 'CSense',
  slug: 'csense-mobile',
  scheme: VARIANT === 'csense' ? 'csense' : `csense-${VARIANT}`,
  version: '1.0.0',
  orientation: 'portrait',
  icon: `./brand/${VARIANT}/icon.png`,
  userInterfaceStyle: 'automatic',
  // A real, supported Expo config field (works today in app.json) - this SDK's own
  // `@expo/config-types` declaration file hasn't caught up to it yet, a known upstream
  // types lag rather than a real config error. The `as ExpoConfig` cast below (not just
  // the function's own return-type annotation) is what actually suppresses the excess-
  // property check this object literal would otherwise fail.
  newArchEnabled: true,
  ios: {
    supportsTablet: true,
    bundleIdentifier: BRAND_IDS[VARIANT] ?? BRAND_IDS.csense,
  },
  android: {
    package: BRAND_IDS[VARIANT] ?? BRAND_IDS.csense,
    adaptiveIcon: {
      backgroundColor: BRAND_BACKGROUND[VARIANT] ?? BRAND_BACKGROUND.csense,
      foregroundImage: `./brand/${VARIANT}/android-icon-foreground.png`,
      backgroundImage: `./brand/${VARIANT}/android-icon-background.png`,
      monochromeImage: `./brand/${VARIANT}/android-icon-monochrome.png`,
    },
    predictiveBackGestureEnabled: false,
  },
  web: {
    favicon: `./brand/${VARIANT}/favicon.png`,
  },
  plugins: [
    'expo-router',
    'expo-secure-store',
    'expo-font',
    ['expo-splash-screen', {
      image: `./brand/${VARIANT}/splash-icon.png`,
      backgroundColor: BRAND_BACKGROUND[VARIANT] ?? BRAND_BACKGROUND.csense,
    }],
  ],
  extra: {
    brandSlug: VARIANT,
    // Created via `eas init --account apti-services` (2026-09-27) - one real EAS
    // project (@apti-services/csense-mobile) serves every brand variant built from
    // this same slug/codebase, not a project per brand.
    eas: { projectId: '5414786d-97dc-4d0c-9a6d-5eeeae661c22' },
  },
} as ExpoConfig);
