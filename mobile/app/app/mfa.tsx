import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { useAuth } from '../src/auth/AuthContext';
import { useColors } from '../src/theme/colors';

export default function MfaScreen() {
  const colors = useColors();
  const { mfaChallenge, verifyMfa, cancelMfa, isLoggingIn, loginError } = useAuth();
  const [code, setCode] = useState('');
  const [useRecoveryCode, setUseRecoveryCode] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  // `Stack.Protected guard={authStage === 'mfaRequired'}` in _layout.tsx means this
  // screen is only ever mounted alongside a real mfaChallenge - but state updates and
  // unmounts don't always land in the same tick, so guard defensively rather than
  // crashing on `mfaChallenge.factor` below.
  if (!mfaChallenge) {
    return (
      <View style={[styles.container, { backgroundColor: colors.surface, justifyContent: 'center' }]}>
        <Text style={[styles.subtitle, { color: colors.textMuted, textAlign: 'center' }]}>
          No sign-in in progress.
        </Text>
      </View>
    );
  }

  const isRecoveryEligible = mfaChallenge.factor === 'totp';
  const factorLabel =
    mfaChallenge.factor === 'totp'
      ? 'Enter the 6-digit code from your authenticator app.'
      : 'Enter the 6-digit code we emailed you.';

  const onSubmit = async () => {
    setValidationError(null);
    const trimmed = code.trim();
    if (useRecoveryCode) {
      if (!trimmed) {
        setValidationError('Enter a recovery code.');
        return;
      }
    } else if (!/^\d{6}$/.test(trimmed)) {
      setValidationError('Enter the 6-digit code.');
      return;
    }
    try {
      await verifyMfa(useRecoveryCode ? { recovery_code: trimmed } : { code: trimmed });
    } catch {
      // `loginError` from useAuth already carries the real server message - the button
      // and error text below already reflect it, nothing further to do here.
    }
  };

  const onCancel = () => {
    setCode('');
    setValidationError(null);
    cancelMfa();
  };

  const shownError = validationError ?? loginError;

  return (
    <KeyboardAvoidingView
      style={[styles.container, { backgroundColor: colors.surface }]}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <View style={styles.form}>
        <Text style={[styles.title, { color: colors.text }]}>Verify it's you</Text>
        <Text style={[styles.subtitle, { color: colors.textMuted }]}>
          {useRecoveryCode ? 'Enter one of your saved recovery codes.' : factorLabel}
        </Text>

        <TextInput
          style={[styles.input, { borderColor: colors.border, color: colors.text }]}
          placeholder={useRecoveryCode ? 'Recovery code' : '000000'}
          placeholderTextColor={colors.textMuted}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType={useRecoveryCode ? 'default' : 'number-pad'}
          maxLength={useRecoveryCode ? undefined : 6}
          value={code}
          onChangeText={setCode}
          testID="mfa-code"
        />

        {shownError ? (
          <Text style={[styles.error, { color: colors.critical }]} testID="mfa-error">
            {shownError}
          </Text>
        ) : null}

        <Pressable
          onPress={onSubmit}
          disabled={isLoggingIn}
          // Crimson, not colors.accent (rose) - crimson is the fill colour for a primary
          // action, rose is for links/focus, matching login.tsx's own convention.
          style={[styles.button, { backgroundColor: colors.crimson, opacity: isLoggingIn ? 0.6 : 1 }]}
          accessibilityRole="button"
          testID="mfa-submit"
        >
          <Text style={[styles.buttonText, { color: colors.textInverse }]}>
            {isLoggingIn ? 'Verifying…' : 'Verify'}
          </Text>
        </Pressable>

        {isRecoveryEligible ? (
          <Pressable
            onPress={() => {
              setUseRecoveryCode((prev) => !prev);
              setCode('');
              setValidationError(null);
            }}
            testID="mfa-toggle-recovery"
          >
            <Text style={[styles.link, { color: colors.accent }]}>
              {useRecoveryCode ? 'Use my authenticator app instead' : 'Use a recovery code instead'}
            </Text>
          </Pressable>
        ) : null}

        <Pressable onPress={onCancel} testID="mfa-cancel">
          <Text style={[styles.link, { color: colors.textMuted }]}>Start over</Text>
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, justifyContent: 'center' },
  form: { padding: 24, gap: 12 },
  title: { fontSize: 28, fontWeight: '700' },
  subtitle: { fontSize: 15, marginBottom: 16 },
  input: { borderWidth: 1, borderRadius: 8, paddingHorizontal: 12, paddingVertical: 10, fontSize: 16 },
  error: { fontSize: 13 },
  button: { borderRadius: 8, paddingVertical: 12, alignItems: 'center', marginTop: 8 },
  buttonText: { color: '#fff', fontWeight: '600', fontSize: 16 },
  link: { fontSize: 14, textAlign: 'center', marginTop: 8 },
});
