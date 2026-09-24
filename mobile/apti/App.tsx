import { CameraView, useCameraPermissions } from 'expo-camera';
import * as ImagePicker from 'expo-image-picker';
import { StatusBar } from 'expo-status-bar';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  AppState,
  Image,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type LayoutChangeEvent,
} from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { detect, DemoError, listModes, Throttled, type DetectResult, type Mode } from './src/api';
import { DetectionOverlay } from './src/components/DetectionOverlay';
import { LIVE_FRAME_WIDTH, LIVE_INTERVAL_MS, UPLOAD_MAX_WIDTH } from './src/config';
import type { Size } from './src/geometry';
import { prepareForUpload } from './src/image';

type Tab = 'live' | 'photo';
type Facing = 'back' | 'front';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export default function App() {
  return (
    <SafeAreaProvider>
      <Screen />
    </SafeAreaProvider>
  );
}

function Screen() {
  const [modes, setModes] = useState<Mode[] | null>(null);
  const [modesError, setModesError] = useState<string | null>(null);
  const [modeCode, setModeCode] = useState('object');
  const [tab, setTab] = useState<Tab>('live');
  const [facing, setFacing] = useState<Facing>('back');
  const [notice, setNotice] = useState<string | null>(null);
  const [result, setResult] = useState<DetectResult | null>(null);
  const [view, setView] = useState<Size>({ width: 0, height: 0 });
  const [photoUri, setPhotoUri] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [appActive, setAppActive] = useState(AppState.currentState === 'active');
  const [permission, requestPermission] = useCameraPermissions();
  const cameraRef = useRef<CameraView>(null);

  const mode = modes?.find((m) => m.code === modeCode) ?? null;

  const loadModes = useCallback(async () => {
    setModesError(null);
    try {
      setModes(await listModes());
    } catch (error) {
      setModesError(error instanceof Error ? error.message : 'Could not load modes.');
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadModes();
  }, [loadModes]);

  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => setAppActive(state === 'active'));
    return () => sub.remove();
  }, []);

  // Live loop: one frame in flight at a time, so a slow network can never pile requests up.
  const live = tab === 'live' && permission?.granted === true && mode?.available === true && appActive;
  useEffect(() => {
    if (!live || !mode) return;
    let cancelled = false;

    (async () => {
      while (!cancelled) {
        const started = Date.now();
        let wait = 0;
        try {
          const picture = await cameraRef.current?.takePictureAsync({
            quality: 0.5,
            shutterSound: false,
            skipProcessing: false,
          });
          if (!picture || cancelled) break;
          const uri = await prepareForUpload(picture.uri, LIVE_FRAME_WIDTH, 0.6);
          const detected = await detect(mode.code, uri);
          if (cancelled) break;
          setResult(detected);
          setNotice(null);
        } catch (error) {
          if (error instanceof Throttled) wait = error.retryAfterMs;
          else {
            setNotice(error instanceof DemoError ? error.message : 'Camera problem. Retrying…');
            wait = 2000;
          }
        }
        await sleep(Math.max(wait, LIVE_INTERVAL_MS - (Date.now() - started)));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [live, mode]);

  function pickMode(code: string) {
    setModeCode(code);
    setResult(null);
    setNotice(null);
    setPhotoUri(null);
  }

  async function choosePhoto() {
    if (!mode?.available) return;
    const picked = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ['images'], quality: 0.8 });
    if (picked.canceled || !picked.assets[0]) return;
    setBusy(true);
    setNotice(null);
    setResult(null);
    try {
      const uri = await prepareForUpload(picked.assets[0].uri, UPLOAD_MAX_WIDTH, 0.7);
      setPhotoUri(uri);
      setResult(await detect(mode.code, uri));
    } catch (error) {
      setNotice(
        error instanceof Throttled
          ? 'The demo is busy. Try again in a moment.'
          : error instanceof Error
            ? error.message
            : 'Detection failed.',
      );
    } finally {
      setBusy(false);
    }
  }

  const onLayout = (e: LayoutChangeEvent) =>
    setView({ width: e.nativeEvent.layout.width, height: e.nativeEvent.layout.height });

  const shown = result && mode && result.mode === mode.code ? result : null;
  const imageSize: Size = shown ? { width: shown.width, height: shown.height } : { width: 0, height: 0 };

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.title}>Apti</Text>
        <Text style={styles.subtitle}>See what AI vision detects, live from your camera.</Text>

        <View style={styles.tabs}>
          {(['live', 'photo'] as Tab[]).map((t) => (
            <Pressable
              key={t}
              onPress={() => {
                setTab(t);
                setResult(null);
                setPhotoUri(null);
                setNotice(null);
              }}
              style={[styles.tab, tab === t && styles.tabActive]}
              accessibilityRole="button"
            >
              <Text style={[styles.tabText, tab === t && styles.tabTextActive]}>
                {t === 'live' ? 'Live camera' : 'Upload photo'}
              </Text>
            </Pressable>
          ))}
        </View>

        {modes === null ? (
          modesError ? (
            <View style={styles.card}>
              <Text style={styles.text}>{modesError}</Text>
              <Pressable onPress={loadModes} style={styles.button}>
                <Text style={styles.buttonText}>Try again</Text>
              </Pressable>
            </View>
          ) : (
            <ActivityIndicator color="#22d3ee" style={{ marginVertical: 24 }} />
          )
        ) : (
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
            {modes.map((m) => (
              <Pressable
                key={m.code}
                onPress={() => pickMode(m.code)}
                style={[styles.chip, m.code === modeCode && styles.chipActive, !m.available && styles.chipDim]}
                accessibilityRole="button"
                accessibilityState={{ selected: m.code === modeCode }}
              >
                <Text style={[styles.chipText, m.code === modeCode && styles.chipTextActive]}>{m.label}</Text>
              </Pressable>
            ))}
          </ScrollView>
        )}

        {mode && <Text style={styles.description}>{mode.description}</Text>}

        {mode && !mode.available ? (
          <View style={styles.card}>
            <Text style={styles.soon}>{mode.note ?? 'Not available yet'}</Text>
            <Text style={styles.text}>This detector is being finished. Try another mode meanwhile.</Text>
          </View>
        ) : tab === 'live' ? (
          !permission ? (
            <ActivityIndicator color="#22d3ee" style={{ marginVertical: 24 }} />
          ) : !permission.granted ? (
            <View style={styles.card}>
              <Text style={styles.text}>Apti needs the camera to show live detection. Frames are analysed and never stored.</Text>
              <Pressable onPress={requestPermission} style={styles.button}>
                <Text style={styles.buttonText}>Allow camera</Text>
              </Pressable>
            </View>
          ) : (
            <>
              <View style={styles.stage} onLayout={onLayout}>
                <CameraView ref={cameraRef} style={StyleSheet.absoluteFill} facing={facing} />
                {shown && (
                  <DetectionOverlay
                    detections={shown.detections}
                    image={imageSize}
                    view={view}
                    fit="cover"
                    mirror={facing === 'front'}
                  />
                )}
              </View>
              <Pressable
                onPress={() => {
                  setFacing((f) => (f === 'back' ? 'front' : 'back'));
                  setResult(null);
                }}
                style={styles.button}
                accessibilityRole="button"
              >
                <Text style={styles.buttonText}>Switch to {facing === 'back' ? 'front' : 'back'} camera</Text>
              </Pressable>
            </>
          )
        ) : (
          <>
            <View style={styles.stage} onLayout={onLayout}>
              {photoUri ? (
                <Image source={{ uri: photoUri }} style={StyleSheet.absoluteFill} resizeMode="contain" />
              ) : (
                <View style={styles.placeholder}>
                  <Text style={styles.text}>Choose a photo to analyse.</Text>
                </View>
              )}
              {shown && photoUri && (
                <DetectionOverlay detections={shown.detections} image={imageSize} view={view} fit="contain" />
              )}
              {busy && (
                <View style={styles.busy}>
                  <ActivityIndicator color="#22d3ee" size="large" />
                </View>
              )}
            </View>
            <Pressable onPress={choosePhoto} disabled={busy} style={[styles.button, busy && { opacity: 0.6 }]}>
              <Text style={styles.buttonText}>{photoUri ? 'Choose another photo' : 'Choose a photo'}</Text>
            </Pressable>
          </>
        )}

        {notice && <Text style={styles.notice}>{notice}</Text>}

        {shown && (
          <View style={styles.card}>
            <Text style={styles.resultTitle}>
              {shown.detections.length === 0
                ? 'Nothing detected'
                : `${shown.detections.length} detected`}
              {'  '}
              <Text style={styles.muted}>{Math.round(shown.inference_ms)} ms</Text>
            </Text>
            {shown.detections.slice(0, 8).map((d, i) => (
              <Text key={i} style={styles.text}>
                {d.label} · {Math.round(d.confidence * 100)}%
              </Text>
            ))}
          </View>
        )}

        <Text style={styles.footer}>
          Photos and camera frames are analysed on the server and are never stored. Faces are detected only, not identified.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { backgroundColor: '#0b1220', flex: 1 },
  content: { gap: 14, padding: 16, paddingBottom: 40 },
  title: { color: '#f8fafc', fontSize: 28, fontWeight: '800' },
  subtitle: { color: '#94a3b8', fontSize: 14, marginTop: -8 },
  tabs: { backgroundColor: '#111a2e', borderRadius: 10, flexDirection: 'row', padding: 4 },
  tab: { alignItems: 'center', borderRadius: 8, flex: 1, paddingVertical: 10 },
  tabActive: { backgroundColor: '#22d3ee' },
  tabText: { color: '#94a3b8', fontWeight: '600' },
  tabTextActive: { color: '#0b1220' },
  chips: { gap: 8, paddingVertical: 2 },
  chip: { backgroundColor: '#111a2e', borderColor: '#1e2a45', borderRadius: 999, borderWidth: 1, paddingHorizontal: 14, paddingVertical: 8 },
  chipActive: { backgroundColor: '#22d3ee', borderColor: '#22d3ee' },
  chipDim: { opacity: 0.55 },
  chipText: { color: '#cbd5e1', fontWeight: '600' },
  chipTextActive: { color: '#0b1220' },
  description: { color: '#94a3b8', fontSize: 13 },
  stage: { aspectRatio: 3 / 4, backgroundColor: '#000', borderRadius: 12, overflow: 'hidden', width: '100%' },
  placeholder: { alignItems: 'center', flex: 1, justifyContent: 'center' },
  busy: { alignItems: 'center', backgroundColor: 'rgba(0,0,0,0.45)', bottom: 0, justifyContent: 'center', left: 0, position: 'absolute', right: 0, top: 0 },
  card: { backgroundColor: '#111a2e', borderRadius: 12, gap: 6, padding: 14 },
  text: { color: '#cbd5e1', fontSize: 14 },
  muted: { color: '#64748b', fontSize: 12 },
  soon: { color: '#fbbf24', fontSize: 16, fontWeight: '700' },
  resultTitle: { color: '#f8fafc', fontSize: 15, fontWeight: '700' },
  button: { alignItems: 'center', backgroundColor: '#1e2a45', borderRadius: 10, paddingVertical: 13 },
  buttonText: { color: '#e2e8f0', fontWeight: '700' },
  notice: { color: '#fbbf24', fontSize: 13 },
  footer: { color: '#64748b', fontSize: 11, marginTop: 8 },
});
