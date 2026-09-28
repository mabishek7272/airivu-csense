import { useCallback, useEffect, useState } from 'react';
import { getSite } from '../api/tenant';
import type { SiteOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  data: SiteOut | null;
  loading: boolean;
  error: string | null;
}

export function useSiteDetail(siteId: string) {
  const [state, setState] = useState<State>({ data: null, loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await getSite(siteId);
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: errorMessage(error) });
    }
  }, [siteId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return { ...state, refresh: load };
}
