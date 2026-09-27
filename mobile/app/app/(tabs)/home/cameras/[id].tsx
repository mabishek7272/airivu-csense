import React from 'react';
import { StyleSheet, View } from 'react-native';
import { useLocalSearchParams } from 'expo-router';
import { useEventListener } from 'expo';
import { useVideoPlayer, VideoView } from 'expo-video';
import { useLiveSession } from '../../../../src/hooks/useLiveSession';
import { LoadingState, ErrorState } from '../../../../src/components/States';

export default function CameraDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { data, loading, error, refresh } = useLiveSession(id);
  const player = useVideoPlayer(data?.play_url ?? null, (p) => {
    p.play();
  });

  // `VideoView` itself has no `onError` prop (confirmed against the installed
  // expo-video types - `VideoViewProps` in `VideoView.types.d.ts` has no error
  // callback at all). Playback errors instead surface on the `player`'s own
  // `statusChange` event (`VideoPlayerEvents` in `VideoPlayerEvents.types.d.ts`), with
  // `status` transitioning to `'error'`. Re-minting the session on that transition
  // (rather than just showing an error) handles MediaMTX's 30-second on-demand-source
  // close after the last viewer disconnects - a stale `play_url` needs a fresh one.
  useEventListener(player, 'statusChange', ({ status }) => {
    if (status === 'error') refresh();
  });

  if (loading && !data) return <LoadingState label="Connecting…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  return (
    <View style={styles.root}>
      <VideoView player={player} style={styles.video} />
    </View>
  );
}

const styles = StyleSheet.create({ root: { flex: 1, backgroundColor: '#000' }, video: { flex: 1 } });
