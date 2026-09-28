import { useCallback, useEffect, useState } from 'react';
import { listSites } from '../api/tenant';
import type { SiteOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  data: SiteOut[];
  loading: boolean;
  error: string | null;
}

export function useSites() {
  const [state, setState] = useState<State>({ data: [], loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await listSites();
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: [], loading: false, error: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return { ...state, refresh: load };
}
