import { MaterialCommunityIcons } from '@expo/vector-icons';
import { CameraView, useCameraPermissions } from 'expo-camera';
import React, { useRef, useState } from 'react';
import { Image, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { submitAuditLogin } from '../api';
import { Btn, Card, Kicker } from '../components/ui';
import { prepareForUpload } from '../image';
import { color, fontHeading, space, textMuted } from '../theme';

// Info-capture only, not a real access gate - the point is recording who tried the demo,
// not keeping anyone out. Sharing this credential publicly does not weaken any real
// security boundary, since there isn't one here.
const DEMO_EMAIL = 'demo.apti@3rdi.in';
const DEMO_PASSWORD = 'Password@123';

function Icon({ name, size = 18, color: c = color.text }: { name: string; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name as never} size={size} color={c} />;
}

type Step = 'details' | 'selfie' | 'credentials';
const STEPS: Step[] = ['details', 'selfie', 'credentials'];

export function LoginScreen({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>('details');
  const [name, setName] = useState('');
  const [place, setPlace] = useState('');
  const [company, setCompany] = useState('');
  const [selfieUri, setSelfieUri] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permission, requestPermission] = useCameraPermissions();
  const cameraRef = useRef<CameraView>(null);

  const stepIndex = STEPS.indexOf(step);
  const detailsValid = name.trim().length > 0 && place.trim().length > 0 && company.trim().length > 0;

  async function takeSelfie() {
    setBusy(true);
    setError(null);
    try {
      const picture = await cameraRef.current?.takePictureAsync({ quality: 0.7, shutterSound: false });
      if (!picture) return;
      const uri = await prepareForUpload(picture.uri, 640, 0.8);
      setSelfieUri(uri);
    } catch {
      setError('Could not take that photo. Try again.');
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!selfieUri) return;
    if (email.trim().toLowerCase() !== DEMO_EMAIL || password !== DEMO_PASSWORD) {
      setError('That email or password is not right.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await submitAuditLogin({ name: name.trim(), place: place.trim(), company: company.trim(), selfieUri });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign in. Try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScrollView contentContainerStyle={styles.content}>
      <View style={{ gap: 2 }}>
        <Kicker>Step {stepIndex + 1} of {STEPS.length}</Kicker>
        <Image source={require('../../assets/apti-logo.png')} style={styles.logo} resizeMode="contain" accessibilityRole="image" accessibilityLabel="Apti" />
      </View>
      <View style={styles.progress}>
        {STEPS.map((s, i) => (
          <View key={s} style={[styles.progressDot, i <= stepIndex && styles.progressDotDone]} />
        ))}
      </View>

      {error && (
        <View style={styles.errorRow}>
          <Icon name="alert-circle-outline" size={16} color={color.accent} />
          <Text style={{ color: color.accent, fontSize: 13, flex: 1 }}>{error}</Text>
        </View>
      )}

      {step === 'details' && (
        <>
          <Text style={styles.title}>Tell us about yourself</Text>
          <Text style={styles.subtitle}>So we know who tried the demo, and from where.</Text>
          <View style={styles.field}>
            <Text style={styles.label}>Name</Text>
            <TextInput value={name} onChangeText={setName} placeholder="Your name" placeholderTextColor={color.neutral400} style={styles.input} />
          </View>
          <View style={styles.field}>
            <Text style={styles.label}>Place</Text>
            <TextInput value={place} onChangeText={setPlace} placeholder="City" placeholderTextColor={color.neutral400} style={styles.input} />
          </View>
          <View style={styles.field}>
            <Text style={styles.label}>Company</Text>
            <TextInput value={company} onChangeText={setCompany} placeholder="Company" placeholderTextColor={color.neutral400} style={styles.input} />
          </View>
          <Btn variant="primary" block disabled={!detailsValid} onPress={() => setStep('selfie')}>
            Next
          </Btn>
        </>
      )}

      {step === 'selfie' && (
        <>
          <Text style={styles.title}>Take a selfie</Text>
          <Text style={styles.subtitle}>One photo, kept with your visit record.</Text>

          {!permission?.granted ? (
            <>
              <Text style={{ color: color.text, fontSize: 14 }}>Camera access is needed to continue.</Text>
              <Btn variant="primary" onPress={requestPermission}>
                Allow camera
              </Btn>
            </>
          ) : selfieUri ? (
            <>
              <Image source={{ uri: selfieUri }} style={styles.selfiePreview} />
              <Btn variant="secondary" block onPress={() => setSelfieUri(null)}>
                Retake
              </Btn>
              <Btn variant="primary" block onPress={() => setStep('credentials')}>
                Next
              </Btn>
            </>
          ) : (
            <>
              <View style={styles.selfieStage}>
                <CameraView ref={cameraRef} style={StyleSheet.absoluteFill} facing="front" />
              </View>
              <Btn variant="primary" block disabled={busy} onPress={takeSelfie}>
                {busy ? 'Capturing…' : 'Take photo'}
              </Btn>
            </>
          )}
        </>
      )}

      {step === 'credentials' && (
        <>
          <Text style={styles.title}>Sign in</Text>
          <Text style={styles.subtitle}>Use the demo credential you were given.</Text>
          <View style={styles.field}>
            <Text style={styles.label}>Email</Text>
            <TextInput
              value={email}
              onChangeText={setEmail}
              placeholder="demo.apti@3rdi.in"
              placeholderTextColor={color.neutral400}
              autoCapitalize="none"
              keyboardType="email-address"
              style={styles.input}
            />
          </View>
          <View style={styles.field}>
            <Text style={styles.label}>Password</Text>
            <TextInput
              value={password}
              onChangeText={setPassword}
              placeholder="Password"
              placeholderTextColor={color.neutral400}
              secureTextEntry
              style={styles.input}
            />
          </View>
          <Card style={{ padding: 12 }}>
            <Text style={{ color: textMuted, fontSize: 12 }}>
              {name.trim()} · {place.trim()} · {company.trim()}
            </Text>
          </Card>
          <Btn variant="primary" block disabled={busy || !email || !password} onPress={submit}>
            {busy ? 'Signing in…' : 'Sign in'}
          </Btn>
        </>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { gap: space[4], padding: 16, paddingBottom: 40, flexGrow: 1 },
  logo: { width: 47.2, height: 48, marginTop: 8 },
  progress: { flexDirection: 'row', gap: 6 },
  progressDot: { flex: 1, height: 3, borderRadius: 2, backgroundColor: color.neutral200 },
  progressDotDone: { backgroundColor: color.accent },
  errorRow: { flexDirection: 'row', gap: 8, alignItems: 'center' },
  title: { fontSize: 20, fontFamily: fontHeading, color: color.text },
  subtitle: { color: textMuted, fontSize: 13, marginTop: -8 },
  field: { gap: 4 },
  label: { fontSize: 12, color: textMuted },
  input: { borderWidth: 1, borderColor: color.divider, borderRadius: 8, padding: 12, fontSize: 15, color: color.text },
  selfieStage: { aspectRatio: 3 / 4, borderRadius: 14, overflow: 'hidden', backgroundColor: color.neutral900 },
  selfiePreview: { width: '100%', aspectRatio: 3 / 4, borderRadius: 14, backgroundColor: color.neutral200 },
});
