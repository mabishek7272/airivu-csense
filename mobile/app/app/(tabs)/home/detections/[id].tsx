import React, { useCallback, useEffect, useState } from 'react';
import { Image, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Link, useLocalSearchParams } from 'expo-router';
import { getDetection } from '../../../../src/api/tenant';
import type { DetectionOut } from '../../../../src/api/types';
import { LoadingState, ErrorState } from '../../../../src/components/States';
import { useColors } from '../../../../src/theme/colors';
import { errorMessage } from '../../../../src/utils/errorMessage';

/** A detection has no status/transition concept (unlike an incident), so this screen is
 * read-only - no Acknowledge/Resolve/Dismiss actions, just the raw record: evidence
 * images, detected objects, camera/location, and a link to the parent incident if one
 * exists. No separate `useDetectionDetail` hook file - there's nothing to mutate, so a
 * plain fetch-on-mount here matches the scope of this screen. */
export default function DetectionDetailScreen() {
  const params = useLocalSearchParams<{ id: string | string[] }>();
  const id = Array.isArray(params.id) ? params.id[0] : params.id;
  const colors = useColors();

  const [data, setData] = useState<DetectionOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const detection = await getDetection(id);
      setData(detection);
    } catch (err) {
      setData(null);
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !data) return <LoadingState label="Loading detection…" />;
  if (error && !data) return <ErrorState message={error} onRetry={load} />;
  if (!data) return null;

  return (
    <ScrollView style={{ backgroundColor: colors.surface }} contentContainerStyle={styles.content}>
      <Text style={[styles.title, { color: colors.text }]}>{data.event_type}</Text>
      <Text style={[styles.meta, { color: colors.textMuted }]}>
        {Math.round(data.confidence * 100)}% confidence · {new Date(data.captured_at).toLocaleString()}
      </Text>

      {data.incident_id ? (
        <Link href={`/(tabs)/home/alerts/${data.incident_id}`} asChild>
          <Text style={[styles.incidentLink, { color: colors.accent }]}>
            Part of incident{data.incident_number ? ` #${data.incident_number}` : ''} →
          </Text>
        </Link>
      ) : null}

      {data.evidence.length > 0 ? (
        <View style={styles.section}>
          <Text style={[styles.sectionTitle, { color: colors.text }]}>Evidence</Text>
          {data.evidence.map((item) =>
            item.url ? (
              <Image
                key={item.evidence_id}
                source={{ uri: item.url }}
                style={[styles.evidenceImage, { backgroundColor: colors.surfaceSunken }]}
                resizeMode="contain"
              />
            ) : (
              <View
                key={item.evidence_id}
                style={[styles.evidenceImage, styles.evidenceMissing, { backgroundColor: colors.surfaceSunken }]}
              >
                <Text style={{ color: colors.textMuted, fontSize: 12 }}>
                  {item.variant} image unavailable
                </Text>
              </View>
            ),
          )}
        </View>
      ) : null}

      <View style={[styles.metaBlock, { borderColor: colors.border }]}>
        <Text style={[styles.sectionTitle, { color: colors.text }]}>Camera &amp; location</Text>
        <Text style={[styles.metaLine, { color: colors.textMuted }]}>Camera: {data.camera.camera_name}</Text>
        <Text style={[styles.metaLine, { color: colors.textMuted }]}>
          Site: {data.location.site_name} ({data.location.site_code})
        </Text>
        {data.location.zone_name ? (
          <Text style={[styles.metaLine, { color: colors.textMuted }]}>Zone: {data.location.zone_name}</Text>
        ) : null}
      </View>

      <View style={styles.section}>
        <Text style={[styles.sectionTitle, { color: colors.text }]}>
          Detected objects ({data.objects.length})
        </Text>
        {data.objects.map((object, index) => (
          <View key={index} style={[styles.objectRow, { borderColor: colors.border }]}>
            <Text style={[styles.objectClass, { color: colors.text }]}>{object.class_name}</Text>
            <Text style={[styles.metaLine, { color: colors.textMuted }]}>
              {Math.round(object.confidence * 100)}% confidence
            </Text>
          </View>
        ))}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { gap: 16, padding: 16 },
  title: { fontSize: 20, fontWeight: '700', textTransform: 'capitalize' },
  meta: { fontSize: 13 },
  incidentLink: { fontSize: 14, fontWeight: '600' },
  section: { gap: 8 },
  sectionTitle: { fontSize: 15, fontWeight: '600' },
  evidenceImage: { borderRadius: 10, height: 220, width: '100%' },
  evidenceMissing: { alignItems: 'center', justifyContent: 'center' },
  metaBlock: { borderRadius: 10, borderWidth: 1, gap: 4, padding: 12 },
  metaLine: { fontSize: 13 },
  objectRow: { borderTopWidth: 1, gap: 2, paddingTop: 8 },
  objectClass: { fontSize: 14, fontWeight: '600', textTransform: 'capitalize' },
});
