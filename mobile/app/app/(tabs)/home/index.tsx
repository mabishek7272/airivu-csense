import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useColors } from '../../../src/theme/colors';
import { useBrand } from '../../../src/theme/BrandProvider';
import { useDashboard } from '../../../src/hooks/useDashboard';
import { CameraIcon, IncidentIcon, SearchIcon, ZoneIcon, type IconProps } from '../../../src/components/Icons';

function StatCard({ label, value, loading }: { label: string; value: number | undefined; loading: boolean }) {
  const colors = useColors();
  return (
    <View style={[styles.statCard, { backgroundColor: colors.raised, borderColor: colors.hair }]}>
      <Text style={[styles.statValue, { color: colors.text }]}>{loading ? '—' : (value ?? 0)}</Text>
      <Text style={[styles.statLabel, { color: colors.textMuted }]}>{label}</Text>
    </View>
  );
}

function Box({
  title,
  subtitle,
  Icon,
  onPress,
}: {
  title: string;
  subtitle?: string;
  Icon: (props: IconProps) => React.ReactElement;
  onPress: () => void;
}) {
  const colors = useColors();
  return (
    <Pressable
      onPress={onPress}
      style={[styles.box, { backgroundColor: colors.raised, borderColor: colors.hair }]}
      accessibilityRole="button"
    >
      <View style={[styles.boxIcon, { backgroundColor: colors.accentSubtle }]}>
        <Icon size={22} color={colors.accent} />
      </View>
      <View style={styles.boxText}>
        <Text style={{ color: colors.text, fontSize: 16, fontFamily: 'Inter_600SemiBold' }}>{title}</Text>
        {subtitle ? (
          <Text style={{ color: colors.textMuted, fontSize: 13, marginTop: 2, fontFamily: 'Inter_400Regular' }}>
            {subtitle}
          </Text>
        ) : null}
      </View>
    </Pressable>
  );
}

export default function HomeScreen() {
  const router = useRouter();
  const brand = useBrand();
  const colors = useColors();
  const { data, loading } = useDashboard();

  return (
    <ScrollView
      style={{ backgroundColor: colors.surface }}
      contentContainerStyle={styles.content}
      showsVerticalScrollIndicator={false}
    >
      {brand.displayName ? (
        <Text style={[styles.heading, { color: colors.text }]}>{brand.displayName}</Text>
      ) : null}

      <View style={styles.statsRow}>
        <StatCard label="Sites" value={data?.sites_count} loading={loading} />
        <StatCard label="Cameras" value={data?.cameras_count} loading={loading} />
        <StatCard label="Open alerts" value={data?.incidents_open_count} loading={loading} />
        <StatCard label="Team" value={data?.team_members_count} loading={loading} />
      </View>

      <View style={styles.boxes}>
        <Box
          title="Camera"
          subtitle={!loading && data ? `${data.cameras_count} camera${data.cameras_count === 1 ? '' : 's'}` : undefined}
          Icon={CameraIcon}
          onPress={() => router.push('/(tabs)/home/cameras')}
        />
        <Box
          title="Alerts"
          subtitle={
            !loading && data
              ? `${data.incidents_open_count} open incident${data.incidents_open_count === 1 ? '' : 's'}`
              : undefined
          }
          Icon={IncidentIcon}
          onPress={() => router.push('/(tabs)/home/alerts')}
        />
        <Box title="Detections" Icon={SearchIcon} onPress={() => router.push('/(tabs)/home/detections')} />
        <Box
          title="Sites"
          subtitle={!loading && data ? `${data.sites_count} site${data.sites_count === 1 ? '' : 's'}` : undefined}
          Icon={ZoneIcon}
          onPress={() => router.push('/(tabs)/home/sites')}
        />
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, gap: 16, paddingBottom: 32 },
  heading: { fontSize: 22, fontFamily: 'Inter_700Bold' },
  statsRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 },
  statCard: { flexBasis: '47%', flexGrow: 1, borderRadius: 12, borderWidth: 1, padding: 14, gap: 4 },
  statValue: { fontSize: 22, fontFamily: 'Inter_700Bold' },
  statLabel: { fontSize: 12, fontFamily: 'Inter_400Regular' },
  boxes: { gap: 12 },
  box: { flexDirection: 'row', alignItems: 'center', gap: 14, padding: 16, borderRadius: 12, borderWidth: 1 },
  boxIcon: { width: 44, height: 44, borderRadius: 10, alignItems: 'center', justifyContent: 'center' },
  boxText: { flex: 1 },
});
