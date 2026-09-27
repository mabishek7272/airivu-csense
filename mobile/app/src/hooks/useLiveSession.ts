import { useCallback, useEffect, useState } from 'react';
import { startLiveSession } from '../api/tenant';
import type { LiveSession } from '../api/tenant';
import { errorMessage } from '../utils/errorMessage';

interface State {
  data: LiveSession | null;
  loading: boolean;
  error: string | null;
}

export function useLiveSession(cameraId: string) {
  const [state, setState] = useState<State>({ data: null, loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await startLiveSession(cameraId);
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: errorMessage(error) });
    }
  }, [cameraId]);

  useEffect(() => {
    // Fetch (or re-mint) the session whenever `cameraId` changes - unlike
    // `useCameras.ts`'s parameterless fetch, `load` here is genuinely reactive to a
    // prop, but the same `react-hooks/set-state-in-effect` shape still applies: there's
    // no props-based alternative to calling `load` here, and `refresh` (the
    // onError-triggered re-mint below) reuses the same function deliberately.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return { ...state, refresh: load };
}
