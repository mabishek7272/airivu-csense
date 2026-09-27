import { useCallback, useEffect, useState } from 'react';
import { getDashboard } from '../api/tenant';
import type { DashboardOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  data: DashboardOut | null;
  loading: boolean;
  error: string | null;
}

/** Backs the Home screen's stats summary and its per-box live counts (Task 13 removed
 * the old top-level Dashboard tab and this hook along with it as dead code; Home's own
 * redesign brings a real consumer back). */
export function useDashboard() {
  const [state, setState] = useState<State>({ data: null, loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await getDashboard();
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return { ...state, refresh: load };
}
