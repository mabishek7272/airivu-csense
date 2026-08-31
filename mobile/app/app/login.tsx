import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { useAuth } from '../src/auth/AuthContext';
import { useColors } from '../src/theme/colors';

export default function LoginScreen() {
  const colors = useColors();
  const { login, isLoggingIn, loginError } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [validationError, setValidationError] = useState<string | null>(null);

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
        <Text style={[styles.title, { color: colors.text }]}>CSense</Text>
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
          style={[styles.button, { backgroundColor: colors.accent, opacity: isLoggingIn ? 0.6 : 1 }]}
          accessibilityRole="button"
          testID="login-submit"
        >
          <Text style={styles.buttonText}>{isLoggingIn ? 'Signing in…' : 'Sign in'}</Text>
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
});
