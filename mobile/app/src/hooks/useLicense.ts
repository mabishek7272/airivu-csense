import { useCallback, useEffect, useState } from 'react';
import { getLicense } from '../api/tenant';
import type { LicenseOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  /** `null` is a real, valid answer (no license assigned yet) - distinguished from
   * "hasn't loaded yet" by `loading`, the same distinction api/tenant.ts's own
   * `getLicense` docstring names. */
  data: LicenseOut | null;
  loading: boolean;
  error: string | null;
}

export function useLicense() {
  const [state, setState] = useState<State>({ data: null, loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await getLicense();
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: errorMessage(error) });
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
