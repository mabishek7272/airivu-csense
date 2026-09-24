import { MaterialCommunityIcons } from '@expo/vector-icons';
import {
  useFonts,
  Inter_400Regular,
  Inter_500Medium,
  Inter_600SemiBold,
} from '@expo-google-fonts/inter';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { LinearGradient } from 'expo-linear-gradient';
import * as ImagePicker from 'expo-image-picker';
import { StatusBar } from 'expo-status-bar';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  AppState,
  Animated,
  Easing,
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
import { Btn, Card, Kicker, Meta, Seg, SegOption, Tag } from './src/components/ui';
import { DetectionOverlay } from './src/components/DetectionOverlay';
import { LIVE_FRAME_WIDTH, LIVE_INTERVAL_MS, UPLOAD_MAX_WIDTH } from './src/config';
import type { Size } from './src/geometry';
import { prepareForUpload } from './src/image';
import { DEFAULT_MODE_ICON, MODE_ICON } from './src/modeIcons';
import { color, fontHeading, fontHeadingSemibold, radius, shadow, space, textMuted, textMuted50 } from './src/theme';

type Tab = 'live' | 'photo';
type Facing = 'back' | 'front';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function Icon({ name, size = 16, color: c = color.text }: { name: string; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name as never} size={size} color={c} />;
}

export default function App() {
  const [fontsLoaded] = useFonts({ Inter_400Regular, Inter_500Medium, Inter_600SemiBold });
  if (!fontsLoaded) {
    return (
      <View style={styles.fontLoading}>
        <ActivityIndicator color={color.accent} />
      </View>
    );
  }
  return (
    <SafeAreaProvider>
      <Screen />
    </SafeAreaProvider>
  );
}

function LiveDot() {
  const pulse = useRef(new Animated.Value(1)).current;
  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 0.35, duration: 700, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 1, duration: 700, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [pulse]);
  return <Animated.View style={[styles.liveDot, { opacity: pulse }]} />;
}

function Spinner({ size = 32 }: { size?: number }) {
  const spin = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    const loop = Animated.loop(Animated.timing(spin, { toValue: 1, duration: 900, easing: Easing.linear, useNativeDriver: true }));
    loop.start();
    return () => loop.stop();
  }, [spin]);
  const rotate = spin.interpolate({ inputRange: [0, 1], outputRange: ['0deg', '360deg'] });
  return (
    <Animated.View style={{ transform: [{ rotate }] }}>
      <Icon name="loading" size={size} color={color.accent} />
    </Animated.View>
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
  const [now, setNow] = useState(new Date());
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

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

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
          if (error instanceof Throttled) {
            wait = error.retryAfterMs;
            setNotice('The demo is busy. Retrying in a moment…');
          } else {
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
  const pad = (n: number) => String(n).padStart(2, '0');

  return (
    <SafeAreaView style={styles.root} edges={['top', 'bottom']}>
      <StatusBar style="light" />
      <LinearGradient
        colors={[color.accent900, color.bg]}
        locations={[0, 0.65]}
        style={StyleSheet.absoluteFill}
        pointerEvents="none"
      />
      <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false}>
        <View style={styles.titleRow}>
          <View style={{ gap: 2 }}>
            <Kicker>Closed circuit assurance</Kicker>
            <Text style={styles.appTitle}>Apti</Text>
          </View>
          {live && (
            <Tag variant="accent" style={styles.liveTag}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <LiveDot />
                <Text style={{ color: color.accent100, fontSize: 11 }}>Live · {(1000 / LIVE_INTERVAL_MS).toFixed(1)} fps</Text>
              </View>
            </Tag>
          )}
        </View>
        <Text style={styles.subtitle}>See what AI vision detects, live from your camera.</Text>

        <Seg>
          <SegOption active={tab === 'live'} first icon={<Icon name="video-outline" size={16} color={tab === 'live' ? color.accent : color.text} />} onPress={() => { setTab('live'); setResult(null); setNotice(null); }}>
            Live camera
          </SegOption>
          <SegOption active={tab === 'photo'} icon={<Icon name="image-outline" size={16} color={tab === 'photo' ? color.accent : color.text} />} onPress={() => { setTab('photo'); setResult(null); setNotice(null); }}>
            Upload photo
          </SegOption>
        </Seg>

        {modes === null ? (
          modesError ? (
            <Card elevated style={{ gap: 10, padding: 14 }}>
              <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
                <Icon name="power-plug-off-outline" size={18} color={color.accent300} />
                <Text style={{ color: color.text, fontSize: 14 }}>Could not reach the demo service.</Text>
              </View>
              <Btn variant="primary" block onPress={loadModes}>
                Try again
              </Btn>
            </Card>
          ) : (
            <View style={{ flexDirection: 'row', gap: 8, paddingVertical: 6 }}>
              {[90, 80, 70, 90].map((w, i) => (
                <View key={i} style={{ height: 36, width: w, borderRadius: 8, backgroundColor: color.surface }} />
              ))}
            </View>
          )
        ) : (
          <>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
              {modes.map((m) => {
                const active = m.code === modeCode;
                return (
                  <Btn
                    key={m.code}
                    variant={active ? 'primary' : 'secondary'}
                    onPress={() => pickMode(m.code)}
                    style={[styles.chip, !m.available && { opacity: 0.55 }]}
                    icon={<Icon name={MODE_ICON[m.code] ?? DEFAULT_MODE_ICON} size={16} color={active ? color.accent : color.text} />}
                  >
                    {m.label}
                  </Btn>
                );
              })}
            </ScrollView>
            {mode && <Text style={styles.modeDescription}>{mode.description}</Text>}
          </>
        )}

        {mode && !mode.available ? (
          <Card elevated style={{ gap: 8, padding: 16 }}>
            <Tag variant="outline">{mode.note ?? 'Not available yet'}</Tag>
            <Text style={{ fontSize: 14, color: color.neutral300 }}>This detector is being finished. Try another mode meanwhile.</Text>
          </Card>
        ) : tab === 'live' ? (
          !permission ? (
            <ActivityIndicator color={color.accent} style={{ marginVertical: 24 }} />
          ) : !permission.granted ? (
            <Card elevated style={{ gap: 12, padding: 18 }}>
              <View style={styles.permissionIcon}>
                <Icon name="camera-outline" size={22} color={color.accent300} />
              </View>
              <Text style={{ fontSize: 14, color: color.neutral300 }}>
                Apti needs the camera to show live detection. Frames are analysed and never stored.
              </Text>
              <Btn variant="primary" block onPress={requestPermission}>
                Allow camera
              </Btn>
            </Card>
          ) : (
            <View style={styles.stage} onLayout={onLayout}>
              <CameraView ref={cameraRef} style={StyleSheet.absoluteFill} facing={facing} />
              {shown && <DetectionOverlay detections={shown.detections} image={imageSize} view={view} fit="cover" mirror={facing === 'front'} />}
              <LinearGradient
                colors={['rgba(22,24,38,0.7)', 'transparent']}
                style={styles.stageTopBar}
                pointerEvents="none"
              >
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                  <View style={styles.camDot} />
                  <Text style={styles.camLabel}>CAM 01 · {facing === 'back' ? 'REAR' : 'FRONT'}</Text>
                </View>
                <Text style={styles.clock}>
                  {now.getFullYear()}-{pad(now.getMonth() + 1)}-{pad(now.getDate())} {pad(now.getHours())}:{pad(now.getMinutes())}:{pad(now.getSeconds())}
                </Text>
              </LinearGradient>
              <Btn
                variant="secondary"
                square={44}
                onPress={() => { setFacing((f) => (f === 'back' ? 'front' : 'back')); setResult(null); }}
                style={styles.flipButton}
                accessibilityLabel="Switch camera"
                icon={<Icon name="camera-flip-outline" size={20} color={color.text} />}
              />
              {busy && (
                <View style={styles.busyOverlay}>
                  <Spinner />
                  <Text style={{ color: color.neutral300, fontSize: 13, marginTop: 10 }}>Analysing…</Text>
                </View>
              )}
            </View>
          )
        ) : (
          <>
            <View style={styles.stage} onLayout={onLayout}>
              {photoUri ? (
                <Image source={{ uri: photoUri }} style={StyleSheet.absoluteFill} resizeMode="contain" />
              ) : (
                <View style={styles.placeholder}>
                  <Text style={{ color: textMuted50 }}>Choose a photo to analyse</Text>
                </View>
              )}
              {shown && photoUri && <DetectionOverlay detections={shown.detections} image={imageSize} view={view} fit="contain" />}
              {busy && (
                <View style={styles.busyOverlay}>
                  <Spinner />
                  <Text style={{ color: color.neutral300, fontSize: 13, marginTop: 10 }}>Analysing…</Text>
                </View>
              )}
            </View>
            <Btn
              variant="primary"
              block
              disabled={busy}
              onPress={choosePhoto}
              icon={<Icon name="upload-outline" size={16} color={color.accent} />}
            >
              {photoUri ? 'Choose another photo' : 'Choose a photo'}
            </Btn>
          </>
        )}

        {notice && (
          <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
            <Icon name="alert-circle-outline" size={16} color={color.accent300} />
            <Text style={{ color: color.accent300, fontSize: 13, flex: 1 }}>{notice}</Text>
          </View>
        )}

        {shown && (
          <Card elevated style={{ padding: 14, gap: 10 }}>
            <View style={{ flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between' }}>
              <Text style={{ fontSize: 15, fontFamily: fontHeading, color: color.text }}>
                {shown.detections.length === 0 ? 'Nothing detected' : `${shown.detections.length} detected`}
              </Text>
              <Meta>
                <Icon name="timer-outline" size={13} color={textMuted50} />
                <Text style={{ color: textMuted50, fontSize: 11 }}>{Math.round(shown.inference_ms)} ms</Text>
              </Meta>
            </View>
            <View style={{ gap: 8 }}>
              {shown.detections.slice(0, 8).map((d, i) => (
                <View key={i} style={styles.resultRow}>
                  <View style={styles.resultLabelRow}>
                    <Text style={styles.resultLabel} numberOfLines={1}>
                      {d.label}
                    </Text>
                    <Text style={styles.resultPct}>{Math.round(d.confidence * 100)}%</Text>
                  </View>
                  <View style={styles.resultTrack}>
                    <View style={[styles.resultFill, { width: `${Math.round(d.confidence * 100)}%` }]} />
                  </View>
                </View>
              ))}
            </View>
          </Card>
        )}

        <View style={styles.footer}>
          <Icon name="shield-check-outline" size={14} color={color.neutral600} />
          <Text style={styles.footerText}>
            Photos and camera frames are analysed on the server and are never stored. Faces are detected only, not identified.
          </Text>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fontLoading: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: color.bg },
  root: { backgroundColor: color.bg, flex: 1 },
  content: { gap: space[4], padding: 16, paddingBottom: 40 },
  titleRow: { flexDirection: 'row', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12 },
  appTitle: { color: color.text, fontSize: 28, fontFamily: fontHeading, letterSpacing: -0.5 },
  liveTag: { paddingVertical: 4 },
  liveDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: color.accent300 },
  subtitle: { color: textMuted, fontSize: 14, marginTop: -6 },
  chips: { gap: 8, paddingVertical: 2 },
  chip: { paddingVertical: 9, paddingHorizontal: 14 },
  modeDescription: { color: textMuted, fontSize: 13, marginTop: -4 },
  permissionIcon: {
    width: 40, height: 40, borderRadius: 8, alignItems: 'center', justifyContent: 'center',
    backgroundColor: color.accent900,
  },
  stage: {
    position: 'relative', width: '100%', aspectRatio: 3 / 4, borderRadius: radius.lg,
    overflow: 'hidden', backgroundColor: color.neutral900, ...shadow.sm, borderWidth: 1, borderColor: color.neutral800,
  },
  placeholder: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  stageTopBar: {
    position: 'absolute', left: 0, right: 0, top: 0, padding: 12,
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
  },
  camDot: { width: 7, height: 7, borderRadius: 3.5, backgroundColor: color.accent400 },
  camLabel: { color: color.text, fontSize: 11, letterSpacing: 1, fontFamily: fontHeadingSemibold },
  clock: { color: color.neutral300, fontSize: 11, fontVariant: ['tabular-nums'] },
  flipButton: { position: 'absolute', right: 12, bottom: 12, borderRadius: 22, backgroundColor: 'rgba(22,24,38,0.7)' },
  busyOverlay: {
    position: 'absolute', inset: 0, alignItems: 'center', justifyContent: 'center',
    backgroundColor: 'rgba(22,24,38,0.6)',
  },
  resultRow: { gap: 4 },
  resultLabelRow: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  resultLabel: { flex: 1, minWidth: 0, color: color.neutral200, fontSize: 14 },
  resultPct: { color: color.neutral400, fontSize: 13, fontVariant: ['tabular-nums'] },
  resultTrack: { width: '100%', height: 2, borderRadius: 1, backgroundColor: color.neutral800, overflow: 'hidden' },
  resultFill: { height: '100%', backgroundColor: color.accent, borderRadius: 1 },
  footer: { flexDirection: 'row', gap: 8, alignItems: 'flex-start', marginTop: 4 },
  footerText: { flex: 1, color: color.neutral600, fontSize: 11, lineHeight: 15 },
});
