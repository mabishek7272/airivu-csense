import { MaterialCommunityIcons } from '@expo/vector-icons';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { File } from 'expo-file-system';
import React, { useEffect, useRef, useState } from 'react';
import { FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { detect, detectFaces } from '../api';
import { Btn, Kicker, Tag } from '../components/ui';
import { useFaceGallery } from '../FaceGalleryContext';
import { addHistoryEntry } from '../history';
import { prepareForUpload } from '../image';
import { LIVE_FRAME_WIDTH } from '../config';
import { TamperDetector } from '../tamperHeuristic';
import { color, fontHeading, space, textMuted } from '../theme';

// Slower than DetectScreen's LIVE_INTERVAL_MS (600ms) on purpose: each cycle here fires
// three server calls (object, phone, face) instead of one, so a 600ms pace would push
// 3 * (1000/600) = 5/s = 300/min per client, well over the server's 150/min cap. At
// 1500ms this is 3 * (1000/1500) = 2/s = 120/min, comfortably under it.
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
  const [elapsedSec, setElapsedSec] = useState(0);
  const cameraRef = useRef<CameraView>(null);
  const tamper = useRef(new TamperDetector()).current;

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setElapsedSec((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [running]);

  useEffect(() => {
    if (!running || !permission?.granted) return;
    let cancelled = false;

    function pushEvent(type: string, label: string, severity: Severity) {
      const event: AlertEvent = { id: `${Date.now()}-${type}`, type, label, severity, time: Date.now() };
      setEvents((prev) => [event, ...prev].slice(0, 100));
      addHistoryEntry({ source: 'monitor', label, confidence: null, severity }).catch(() => {});
    }

    (async () => {
      while (!cancelled) {
        const started = Date.now();
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

          const people = objects.detections.filter((d) => d.label === 'Person').length;
          if (people > 1) pushEvent('multiple_people', `${people} people in frame`, 'medium');

          if (phone.detections.length > 0) pushEvent('phone', 'Mobile phone detected', 'high');

          for (const face of faces.faces) {
            const match = gallery.match(face.embedding);
            if (!match) pushEvent('unknown_face', 'Unknown face detected', 'medium');
          }
        } catch {
          // A single failed cycle is not fatal - the next one just tries again. Promise.all
          // means a throttled/failed call discards the whole cycle's detections rather than
          // committing a partial result, so there's nothing to roll back here.
        }
        const wait = CYCLE_MS - (Date.now() - started);
        await new Promise((resolve) => setTimeout(resolve, Math.max(wait, 200)));
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
  }, [running, permission?.granted, gallery.match]);

  function start() {
    tamper.reset();
    setEvents([]);
    setElapsedSec(0);
    setRunning(true);
  }

  function stop() {
    setRunning(false);
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
                <Text style={styles.statusText}>{events.length} alert{events.length === 1 ? '' : 's'}</Text>
              </View>
            )}
          </View>

          <Btn variant="primary" block onPress={running ? stop : start}>
            {running ? 'Stop' : 'Run'}
          </Btn>

          {!running && events.length > 0 && (
            <View style={styles.summary}>
              <Text style={{ color: color.text, fontFamily: fontHeading, fontSize: 15 }}>Session summary</Text>
              <Text style={{ color: textMuted, fontSize: 13 }}>
                {mm}:{ss} · {events.length} alert{events.length === 1 ? '' : 's'}
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
