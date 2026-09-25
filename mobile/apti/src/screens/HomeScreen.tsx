// mobile/apti/src/screens/HomeScreen.tsx
import { MaterialCommunityIcons } from '@expo/vector-icons';
import React, { useCallback, useEffect, useState } from 'react';
import { Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Card, Kicker, Tag } from '../components/ui';
import { useFaceGallery } from '../FaceGalleryContext';
import { loadHistory, type HistoryEntry } from '../history';
import { color, fontHeading, space, textMuted } from '../theme';

function Icon({ name, size = 22, color: c = color.accent }: { name: string; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name as never} size={size} color={c} />;
}

function ActionCard({
  icon,
  title,
  description,
  primary,
  onPress,
}: {
  icon: string;
  title: string;
  description: string;
  primary?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button">
      <Card elevated style={[styles.actionCard, primary && styles.actionCardPrimary]}>
        <Icon name={icon} size={26} color={primary ? color.accent : color.text} />
        <Text style={styles.actionTitle}>{title}</Text>
        <Text style={styles.actionDescription}>{description}</Text>
      </Card>
    </Pressable>
  );
}

export function HomeScreen({
  onRegister,
  onDetect,
  onMonitor,
}: {
  onRegister: () => void;
  onDetect: () => void;
  onMonitor: () => void;
}) {
  const gallery = useFaceGallery();
  const [recent, setRecent] = useState<HistoryEntry[]>([]);

  const refresh = useCallback(() => {
    loadHistory().then((entries) => setRecent(entries.slice(0, 10)));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <ScrollView contentContainerStyle={styles.content}>
      <View style={{ gap: 2 }}>
        <Kicker>Closed circuit assurance</Kicker>
        <Image source={require('../../assets/apti-logo.png')} style={styles.logo} resizeMode="contain" accessibilityLabel="Apti" />
      </View>
      <View style={styles.statusRow}>
        <Tag variant="neutral">Models ready</Tag>
        {gallery.personCount > 0 && <Tag variant="accent">{gallery.personCount} registered this session</Tag>}
      </View>

      <ActionCard
        icon="account-plus-outline"
        title="Register a Face"
        description="Add yourself for this session only."
        onPress={onRegister}
      />
      <ActionCard icon="magnify-scan" title="Try a Model" description="Live camera or upload a photo." onPress={onDetect} />
      <ActionCard
        icon="cctv"
        title="Live Monitoring"
        description="Watch continuously for unknown faces, phones and more."
        primary
        onPress={onMonitor}
      />

      <Text style={styles.sectionTitle}>Recent</Text>
      {recent.length === 0 ? (
        <Text style={{ color: textMuted, fontSize: 13 }}>No detections yet. Try a model to get started.</Text>
      ) : (
        recent.map((entry) => (
          <View key={entry.id} style={styles.recentRow}>
            <Text style={{ color: color.text, fontSize: 13, flex: 1 }}>{entry.label}</Text>
            <Text style={{ color: textMuted, fontSize: 11 }}>{new Date(entry.timestamp).toLocaleTimeString()}</Text>
          </View>
        ))
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { gap: space[4], padding: 16, paddingBottom: 40 },
  logo: { width: 47.2, height: 48 },
  statusRow: { flexDirection: 'row', gap: 8 },
  actionCard: { padding: 16, gap: 6 },
  actionCardPrimary: { borderColor: color.accent, borderWidth: 1 },
  actionTitle: { fontSize: 16, fontFamily: fontHeading, color: color.text },
  actionDescription: { fontSize: 13, color: textMuted },
  sectionTitle: { fontSize: 13, fontFamily: fontHeading, color: color.text, marginTop: 8 },
  recentRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 6 },
});
