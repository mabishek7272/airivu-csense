import React, { useEffect, useState } from 'react';
import {
  Animated,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useAuth } from '../src/auth/AuthContext';
import { BRAND_LOCAL_LOGO, useBrand } from '../src/theme/BrandProvider';
import { useColors } from '../src/theme/colors';

export default function LoginScreen() {
  const colors = useColors();
  const brand = useBrand();
  // A remote, tenant-fetched logo wins once it resolves; until then (or for a brand
  // with no org_branding row at all, like CSense - see BrandProvider.tsx's own
  // comment) this build's own bundled mark shows instead of nothing.
  const logoSource = brand.logoUrl ? { uri: brand.logoUrl } : BRAND_LOCAL_LOGO;
  const { login, isLoggingIn, loginError } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [validationError, setValidationError] = useState<string | null>(null);

  // Splash-to-login entrance: the logo starts large and low (echoing where the native
  // splash screen leaves it - centered, prominent) and settles into its smaller header
  // position while the form fades and slides in just after. Built with React Native's
  // own Animated API - no new native dependency, so it can't be the reason a future EAS
  // build fails the way the first Apti build did (an unrelated npm peer-dependency
  // issue, not a design choice - see .npmrc's own comment). Runs once per mount.
  //
  // `useState(() => new Animated.Value(...))` rather than `useRef(...).current` - this
  // repo's eslint config enforces React's newer "no ref access during render" rule,
  // which flags reading a ref's `.current` anywhere in the component body (including
  // just to hand it to a style prop); a lazily-initialized state value is exempt from
  // that rule and, since nothing here ever calls its own setter, behaves identically to
  // a ref in practice - one stable Animated.Value per mount, never replaced by a
  // re-render.
  const [logoScale] = useState(() => new Animated.Value(1.8));
  const [logoTranslateY] = useState(() => new Animated.Value(72));
  const [formOpacity] = useState(() => new Animated.Value(0));
  const [formTranslateY] = useState(() => new Animated.Value(18));

  useEffect(() => {
    Animated.sequence([
      Animated.parallel([
        Animated.spring(logoScale, { toValue: 1, friction: 7, tension: 40, useNativeDriver: true }),
        Animated.spring(logoTranslateY, { toValue: 0, friction: 7, tension: 40, useNativeDriver: true }),
      ]),
      Animated.parallel([
        Animated.timing(formOpacity, { toValue: 1, duration: 320, useNativeDriver: true }),
        Animated.timing(formTranslateY, { toValue: 0, duration: 320, useNativeDriver: true }),
      ]),
    ]).start();
  }, [logoScale, logoTranslateY, formOpacity, formTranslateY]);

  const onSubmit = async () => {
    setValidationError(null);
    const trimmedEmail = email.trim();
    if (!trimmedEmail || !password) {
      setValidationError('Enter your email and password.');
      return;
    }
    try {
      await login(trimmedEmail, password);
    } catch {
      // `loginError` from useAuth already carries the real server message - the button
      // and error text below already reflect it, nothing further to do here.
    }
  };

  const shownError = validationError ?? loginError;

  return (
    <KeyboardAvoidingView
      style={[styles.container, { backgroundColor: colors.surface }]}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <View style={styles.form}>
        <Animated.View
          style={{ transform: [{ scale: logoScale }, { translateY: logoTranslateY }] }}
        >
          {logoSource ? <Image source={logoSource} style={styles.logo} resizeMode="contain" /> : null}
          {brand.displayName ? (
            <Text style={[styles.title, { color: colors.text }]}>{brand.displayName}</Text>
          ) : null}
        </Animated.View>

        <Animated.View style={{ opacity: formOpacity, transform: [{ translateY: formTranslateY }], gap: 12 }}>
          <Text style={[styles.subtitle, { color: colors.textMuted }]}>Sign in to your account</Text>

          <TextInput
            style={[styles.input, { borderColor: colors.border, color: colors.text }]}
            placeholder="Email"
            placeholderTextColor={colors.textMuted}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="email-address"
            textContentType="username"
            value={email}
            onChangeText={setEmail}
            testID="login-email"
          />
          <TextInput
            style={[styles.input, { borderColor: colors.border, color: colors.text }]}
            placeholder="Password"
            placeholderTextColor={colors.textMuted}
            secureTextEntry
            textContentType="password"
            value={password}
            onChangeText={setPassword}
            testID="login-password"
          />

          {shownError ? (
            <Text style={[styles.error, { color: colors.critical }]} testID="login-error">
              {shownError}
            </Text>
          ) : null}

          <Pressable
            onPress={onSubmit}
            disabled={isLoggingIn}
            // Crimson, not colors.accent (rose) - crimson is the fill colour for a primary
            // action, rose is for links/focus/attention-that-has-to-read-as-text. Same rule
            // the web CRM's own button.primary follows (System.dc.html's own usage note).
            style={[styles.button, { backgroundColor: colors.crimson, opacity: isLoggingIn ? 0.6 : 1 }]}
            accessibilityRole="button"
            testID="login-submit"
          >
            <Text style={[styles.buttonText, { color: colors.textInverse }]}>
              {isLoggingIn ? 'Signing in…' : 'Sign in'}
            </Text>
          </Pressable>
        </Animated.View>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, justifyContent: 'center' },
  form: { padding: 24, gap: 12 },
  logo: { width: 72, height: 72, marginBottom: 12 },
  title: { fontSize: 28, fontFamily: 'Inter_700Bold' },
  subtitle: { fontSize: 15, marginBottom: 16, fontFamily: 'Inter_400Regular' },
  input: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 16,
    fontFamily: 'Inter_400Regular',
  },
  error: { fontSize: 13, fontFamily: 'Inter_400Regular' },
  button: { borderRadius: 8, paddingVertical: 12, alignItems: 'center', marginTop: 8 },
  buttonText: { color: '#fff', fontSize: 16, fontFamily: 'Inter_600SemiBold' },
});
