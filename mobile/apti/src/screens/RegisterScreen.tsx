import { MaterialCommunityIcons } from '@expo/vector-icons';
import * as ImagePicker from 'expo-image-picker';
import React, { useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { detectFaces } from '../api';
import { Btn, Card, Kicker, Tag } from '../components/ui';
import { useFaceGallery } from '../FaceGalleryContext';
import { prepareForUpload } from '../image';
import { color, fontHeading, space, textMuted } from '../theme';

const MAX_PHOTOS = 5;
const MIN_PHOTOS = 1;

function Icon({ name, size = 18, color: c = color.text }: { name: string; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name as never} size={size} color={c} />;
}

type Step = 'details' | 'capture' | 'consent' | 'review';

type CapturedPhoto = { uri: string; embedding: number[] };

export function RegisterScreen({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const gallery = useFaceGallery();
  const [step, setStep] = useState<Step>('details');
  const [name, setName] = useState('');
  const [photos, setPhotos] = useState<CapturedPhoto[]>([]);
  const [consented, setConsented] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const stepIndex = (['details', 'capture', 'consent', 'review'] as Step[]).indexOf(step);

  async function addPhoto() {
    const picked = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ['images'],
      quality: 0.85,
      cameraType: ImagePicker.CameraType.front,
    });
    if (picked.canceled || !picked.assets[0]) return;
    setBusy(true);
    setError(null);
    try {
      const uri = await prepareForUpload(picked.assets[0].uri, 640, 0.8);
      const result = await detectFaces(uri);
      if (result.faces.length === 0) {
        setError('No face was found in that photo. Try better lighting, facing the camera directly.');
        return;
      }
      if (result.faces.length > 1) {
        setError('More than one face was found. Use a photo of just yourself.');
        return;
      }
      const embedding = result.faces[0].embedding;
      const duplicate = gallery.findDuplicate(embedding);
      if (duplicate && duplicate.name !== name.trim()) {
        Alert.alert(
          'Already registered?',
          `This looks like ${duplicate.name} (${Math.round(duplicate.similarity * 100)}% match). Add it anyway?`,
          [
            { text: 'Cancel', style: 'cancel' },
            { text: 'Add anyway', onPress: () => setPhotos((prev) => [...prev, { uri, embedding }]) },
          ],
        );
        return;
      }
      setPhotos((prev) => [...prev, { uri, embedding }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not check that photo. Try again.');
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      // Embeddings were already computed (and validated) when each photo was added in
      // addPhoto() - reuse them here instead of re-calling detectFaces. Registering only
      // happens after every photo is accounted for, under a single id, so a failure can
      // never leave partial/orphaned entries in the gallery.
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      for (const photo of photos) gallery.register(id, name.trim(), photo.embedding);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save. Try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScrollView contentContainerStyle={styles.content}>
      <View style={styles.header}>
        <Pressable onPress={onBack} accessibilityRole="button" accessibilityLabel="Back">
          <Icon name="arrow-left" />
        </Pressable>
        <Kicker>Step {stepIndex + 1} of 4</Kicker>
      </View>
      <View style={styles.progress}>
        {(['details', 'capture', 'consent', 'review'] as Step[]).map((s, i) => (
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
          <Text style={styles.title}>Your name</Text>
          <Text style={styles.subtitle}>Shown only in this session's results - never stored anywhere else.</Text>
          <View style={styles.field}>
            <Text style={styles.label}>Name</Text>
            <TextInput
              value={name}
              onChangeText={setName}
              placeholder="Your name"
              placeholderTextColor={color.neutral400}
              style={styles.input}
            />
          </View>
          <Btn variant="primary" block disabled={name.trim().length === 0} onPress={() => setStep('capture')}>
            Next
          </Btn>
        </>
      )}

      {step === 'capture' && (
        <>
          <Text style={styles.title}>Add photos</Text>
          <Text style={styles.subtitle}>
            One clear, front-facing photo per angle. {photos.length}/{MAX_PHOTOS} added.
          </Text>
          <View style={styles.photoGrid}>
            {photos.map((p, i) => (
              <Tag key={i} variant="neutral">
                {`Photo ${i + 1}`}
              </Tag>
            ))}
          </View>
          <Btn variant="secondary" block disabled={busy || photos.length >= MAX_PHOTOS} onPress={addPhoto}>
            {busy ? 'Checking…' : 'Add a photo'}
          </Btn>
          <Btn variant="primary" block disabled={photos.length < MIN_PHOTOS} onPress={() => setStep('consent')}>
            Next
          </Btn>
        </>
      )}

      {step === 'consent' && (
        <>
          <Text style={styles.title}>Consent</Text>
          <Card style={{ padding: 16, gap: 10 }}>
            <Text style={{ color: color.text, fontSize: 14 }}>
              Your face will be used only for this demo session. It is never stored, uploaded to a database, or
              kept after you close the app - only a temporary match embedding is held in this device's memory
              until then.
            </Text>
            <Pressable style={styles.consentRow} onPress={() => setConsented((c) => !c)} accessibilityRole="checkbox" accessibilityState={{ checked: consented }}>
              <Icon name={consented ? 'checkbox-marked' : 'checkbox-blank-outline'} color={consented ? color.accent : color.neutral600} />
              <Text style={{ color: color.text, fontSize: 14, flex: 1 }}>I agree to try this demo with my own face.</Text>
            </Pressable>
          </Card>
          <Btn variant="primary" block disabled={!consented} onPress={() => setStep('review')}>
            Next
          </Btn>
        </>
      )}

      {step === 'review' && (
        <>
          <Text style={styles.title}>Review</Text>
          <Card style={{ padding: 16, gap: 6 }}>
            <Text style={{ color: color.text, fontSize: 15, fontFamily: fontHeading }}>{name.trim()}</Text>
            <Text style={{ color: textMuted, fontSize: 13 }}>{photos.length} photo(s) - session only</Text>
          </Card>
          <Btn variant="primary" block disabled={busy} onPress={save}>
            {busy ? 'Saving…' : 'Save profile'}
          </Btn>
        </>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { gap: space[4], padding: 16, paddingBottom: 40 },
  header: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  progress: { flexDirection: 'row', gap: 6 },
  progressDot: { flex: 1, height: 3, borderRadius: 2, backgroundColor: color.neutral200 },
  progressDotDone: { backgroundColor: color.accent },
  errorRow: { flexDirection: 'row', gap: 8, alignItems: 'center' },
  title: { fontSize: 20, fontFamily: fontHeading, color: color.text },
  subtitle: { color: textMuted, fontSize: 13, marginTop: -8 },
  field: { gap: 4 },
  label: { fontSize: 12, color: textMuted },
  photoGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  consentRow: { flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 4 },
  input: { borderWidth: 1, borderColor: color.divider, borderRadius: 8, padding: 12, fontSize: 15, color: color.text },
});
