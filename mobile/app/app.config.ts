import type { ExpoConfig } from 'expo/config';

const VARIANT = process.env.APP_VARIANT ?? 'csense';

const BRAND_NAMES: Record<string, string> = {
  apti: 'Apti', eaigleye: 'eAIgleye', '3rdi': '3RDi', csense: 'CSense',
};

const BRAND_IDS: Record<string, string> = {
  apti: 'ai.airivu.apti.client', eaigleye: 'ai.airivu.eaigleye', '3rdi': 'ai.airivu.threerdi', csense: 'ai.airivu.csense',
};

const BRAND_BACKGROUND: Record<string, string> = {
  apti: '#f3f5fe', eaigleye: '#0F2A43', '3rdi': '#0F2A43', csense: '#0F2A43',
};

export default (): ExpoConfig => ({
  name: BRAND_NAMES[VARIANT] ?? 'CSense',
  slug: 'csense-mobile',
  scheme: `csense-${VARIANT}`,
  version: '1.0.0',
  orientation: 'portrait',
  icon: `./brand/${VARIANT}/icon.png`,
  userInterfaceStyle: 'automatic',
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
    ['expo-splash-screen', {
      image: `./brand/${VARIANT}/splash-icon.png`,
      backgroundColor: BRAND_BACKGROUND[VARIANT] ?? BRAND_BACKGROUND.csense,
    }],
  ],
  extra: { brandSlug: VARIANT },
});
