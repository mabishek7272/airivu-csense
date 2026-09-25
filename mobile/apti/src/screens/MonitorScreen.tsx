import { MaterialCommunityIcons } from '@expo/vector-icons';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { File } from 'expo-file-system';
import React, { useEffect, useRef, useState } from 'react';
import { AppState, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { DemoError, Throttled, detect, detectFaces } from '../api';
import { Btn, Kicker, Tag } from '../components/ui';
import { useFaceGallery } from '../FaceGalleryContext';
import { addHistoryEntry } from '../history';
import { prepareForUpload } from '../image';
import { LIVE_FRAME_WIDTH } from '../config';
import { TamperDetector } from '../tamperHeuristic';
import { color, fontHeading, space, textMuted } from '../theme';

// Slower than DetectScreen's LIVE_INTERVAL_MS (600ms) on purpose: each cycle here fires
// three concurrent server calls (object, phone, face) instead of one. The per-IP cap
// (150/min) isn't actually the binding constraint - 3 * (1000/1500) = 120/min clears it
// with room to spare. The tighter constraint is the server's *global* in-flight semaphore,
// MAX_IN_FLIGHT=4 in backend/demo_api/app/main.py, shared across every demo user/screen
// AND the same ai-runtime container that also serves real tenant camera inference. Each
// 1.5s cycle's 3 concurrent requests occupy 3 of those 4 global slots for the cycle's
// duration - there is no comfortable headroom here, just less contention than a faster
// pace would cause.
const CYCLE_MS = 1500;

type Severity = 'low' | 'medium' | 'high';

interface AlertEvent {
  id: string;
  type: string;
  label: string;
  severity: Severity;
  time: number;
}

const SEVERITY_TAG: Record<Severity, 'outline' | 'accent' | 'neutral'> = {
  low: 'neutral',
  medium: 'outline',
  high: 'accent',
};

function Icon({ name, size = 18, color: c = color.text }: { name: string; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name as never} size={size} color={c} />;
}

export function MonitorScreen({ onBack }: { onBack: () => void }) {
  const gallery = useFaceGallery();
  const [permission, requestPermission] = useCameraPermissions();
  const [running, setRunning] = useState(false);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  // Separate from `events.length`: `events` is capped at 100 for the FlatList, but the
  // live badge and the session summary need the true count of everything that happened
  // in the session, not just what's still in the capped list.
  const [totalAlertCount, setTotalAlertCount] = useState(0);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [appActive, setAppActive] = useState(AppState.currentState === 'active');
  const [notice, setNotice] = useState<string | null>(null);
  const cameraRef = useRef<CameraView>(null);
  const tamper = useRef(new TamperDetector()).current;
  // Monotonic, not reset per effect run: Date.now() alone is only ms-resolution, and this
  // loop's body is synchronous between awaits, so 2+ unknown faces (or any two pushEvent
  // calls) in the same frame would otherwise get identical ids and break FlatList's
  // keyExtractor.
  const eventIdCounter = useRef(0);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setElapsedSec((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [running]);

  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => setAppActive(state === 'active'));
    return () => sub.remove();
  }, []);

  // If OS camera permission is revoked mid-session, end the session outright rather than
  // just pausing the loop below - otherwise a re-grant before Stop would let the loop
  // silently resume mid-effect without going through start(), continuing what looks like
  // the same session across a permission gap (no tamper.reset()/setEvents([])). Requiring
  // a fresh Start after a permission gap is the safer, cheap fix.
  useEffect(() => {
    if (running && permission?.granted === false) stop();
  }, [running, permission?.granted]);

  useEffect(() => {
    if (!running || !permission?.granted || !appActive) return;
    let cancelled = false;

    function pushEvent(type: string, label: string, severity: Severity, confidence: number | null = null) {
      const id = `${Date.now()}-${type}-${eventIdCounter.current++}`;
      const event: AlertEvent = { id, type, label, severity, time: Date.now() };
      setEvents((prev) => [event, ...prev].slice(0, 100));
      setTotalAlertCount((n) => n + 1);
      addHistoryEntry({ source: 'monitor', label, confidence, severity }).catch(() => {});
    }

    (async () => {
      while (!cancelled) {
        const started = Date.now();
        let wait = 0;
        try {
          const picture = await cameraRef.current?.takePictureAsync({ quality: 0.5, shutterSound: false });
          if (!picture || cancelled) break;
          const uri = await prepareForUpload(picture.uri, LIVE_FRAME_WIDTH, 0.6);
          if (cancelled) break;

          const size = new File(uri).size;
          if (tamper.check(size)) pushEvent('camera_tampered', 'Camera may be blocked', 'high');

          // Three real, independent rules per cycle. `phone` is its own server mode (not a
          // client-side filter of `object`'s results) - it runs at min_confidence 0.25
          // server-side vs. `object`'s 0.35 (backend/demo_api/app/modes.py), so deriving
          // "phone" from `object`'s detections would silently raise its threshold and miss
          // detections the dedicated mode would catch.
          const [objects, phone, faces] = await Promise.all([
            detect('object', uri),
            detect('phone', uri),
            detectFaces(uri),
          ]);
          if (cancelled) break;

          const personDetections = objects.detections.filter((d) => d.label === 'Person');
          if (personDetections.length > 1) {
            const topConfidence = Math.max(...personDetections.map((d) => d.confidence));
            pushEvent('multiple_people', `${personDetections.length} people in frame`, 'medium', topConfidence);
          }

          if (phone.detections.length > 0) {
            const topConfidence = Math.max(...phone.detections.map((d) => d.confidence));
            pushEvent('phone', 'Mobile phone detected', 'high', topConfidence);
          }

          for (const face of faces.faces) {
            const match = gallery.match(face.embedding);
            if (!match) pushEvent('unknown_face', 'Unknown face detected', 'medium', face.detectorConfidence);
          }
          setNotice(null);
        } catch (error) {
          // Unlike a bare swallow, distinguish a Throttled/busy response (back off by the
          // server's own retryAfterMs and say so) from any other failure, mirroring
          // DetectScreen.tsx's live loop - so a run of 503s is visibly different from a
          // healthy-but-quiet monitoring session instead of looking identical to one.
          if (error instanceof Throttled) {
            wait = error.retryAfterMs;
            setNotice('The demo is busy. Retrying in a moment…');
          } else {
            setNotice(error instanceof DemoError ? error.message : 'Camera problem. Retrying…');
            wait = 2000;
          }
          // A single failed cycle is not fatal - the next one just tries again. Promise.all
          // means a throttled/failed call discards the whole cycle's detections rather than
          // committing a partial result, so there's nothing to roll back here.
        }
        // Floor of 200ms regardless of outcome, so a cycle that overran CYCLE_MS (wait
        // negative) or a caught error with wait left at 0 can't collapse into a tight
        // polling loop against the server.
        await new Promise((resolve) => setTimeout(resolve, Math.max(wait, CYCLE_MS - (Date.now() - started), 200)));
      }
    })();

    return () => {
      cancelled = true;
    };
    // gallery.match is useCallback-memoized on the gallery's [entries] (FaceGalleryContext.tsx),
    // so this only restarts the loop when the registered faces actually change, not on
    // unrelated renders - matching the fix already made for this exact staleness risk in
    // DetectScreen.tsx (commit 4147b00). Without it, a face registered while a monitoring
    // session is already running would keep matching against a stale, closed-over gallery
    // for the rest of that session.
  }, [running, permission?.granted, appActive, gallery.match]);

  function start() {
    tamper.reset();
    setEvents([]);
    setTotalAlertCount(0);
    setElapsedSec(0);
    setNotice(null);
    setRunning(true);
  }

  function stop() {
    setRunning(false);
    setNotice(null);
  }

  const pad = (n: number) => String(n).padStart(2, '0');
  const mm = pad(Math.floor(elapsedSec / 60));
  const ss = pad(elapsedSec % 60);

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Pressable onPress={onBack} accessibilityRole="button" accessibilityLabel="Back">
          <Icon name="arrow-left" />
        </Pressable>
        <Kicker>Live Monitoring</Kicker>
      </View>

      {!permission?.granted ? (
        <View style={styles.centered}>
          <Text style={{ color: color.text, fontSize: 14, textAlign: 'center', marginBottom: 12 }}>
            Camera access is needed to monitor.
          </Text>
          <Btn variant="primary" onPress={requestPermission}>
            Allow camera
          </Btn>
        </View>
      ) : (
        <>
          <View style={styles.stage}>
            <CameraView ref={cameraRef} style={StyleSheet.absoluteFill} facing="back" />
            {running && (
              <View style={styles.statusBar}>
                <Text style={styles.statusText}>{mm}:{ss}</Text>
                <Text style={styles.statusText}>
                  {totalAlertCount} alert{totalAlertCount === 1 ? '' : 's'}
                </Text>
              </View>
            )}
          </View>

          <Btn variant="primary" block onPress={running ? stop : start}>
            {running ? 'Stop' : 'Run'}
          </Btn>

          {running && notice && (
            <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
              <Icon name="alert-circle-outline" size={16} color={color.accent} />
              <Text style={{ color: color.accent, fontSize: 13, flex: 1 }}>{notice}</Text>
            </View>
          )}

          {!running && totalAlertCount > 0 && (
            <View style={styles.summary}>
              <Text style={{ color: color.text, fontFamily: fontHeading, fontSize: 15 }}>Session summary</Text>
              <Text style={{ color: textMuted, fontSize: 13 }}>
                {mm}:{ss} · {totalAlertCount} alert{totalAlertCount === 1 ? '' : 's'}
              </Text>
            </View>
          )}

          <FlatList
            data={events}
            keyExtractor={(e) => e.id}
            style={{ flex: 1 }}
            renderItem={({ item }) => (
              <View style={styles.eventRow}>
                <Tag variant={SEVERITY_TAG[item.severity]}>{item.severity.toUpperCase()}</Tag>
                <Text style={{ color: color.text, fontSize: 13, flex: 1 }}>{item.label}</Text>
              </View>
            )}
          />
        </>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: color.bg, padding: 16, gap: space[4] },
  header: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  centered: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  stage: { aspectRatio: 3 / 4, borderRadius: 14, overflow: 'hidden', backgroundColor: color.neutral900 },
  statusBar: {
    position: 'absolute', left: 0, right: 0, top: 0, padding: 10,
    flexDirection: 'row', justifyContent: 'space-between', backgroundColor: 'rgba(22,24,38,0.6)',
  },
  statusText: { color: '#e9e9ed', fontSize: 12 },
  summary: { padding: 14, borderRadius: 8, backgroundColor: color.surface, gap: 4 },
  eventRow: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 8 },
});
