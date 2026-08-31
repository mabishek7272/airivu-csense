import { useCallback, useEffect, useState } from 'react';
import { listCameras } from '../api/tenant';
import type { CameraOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  data: CameraOut[];
  loading: boolean;
  error: string | null;
}

export function useCameras() {
  const [state, setState] = useState<State>({ data: [], loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await listCameras();
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: [], loading: false, error: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    // Fetch once when this screen mounts - see useDashboard.ts's own comment on this
    // same, deliberate `react-hooks/set-state-in-effect` suppression.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return { ...state, refresh: load };
}
