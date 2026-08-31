import { useCallback, useEffect, useState } from 'react';
import {
  acknowledgeIncident,
  dismissIncident,
  getIncident,
  investigateIncident,
  resolveIncident,
} from '../api/tenant';
import type { IncidentDetail } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  data: IncidentDetail | null;
  loading: boolean;
  error: string | null;
  /** Set only while a transition (acknowledge/investigate/resolve/dismiss) call is in
   * flight - distinct from `loading`, which is only the initial/refresh fetch, so the
   * detail screen can disable its action buttons without blanking the whole screen. */
  transitioning: boolean;
  transitionError: string | null;
}

export function useIncidentDetail(incidentId: string) {
  const [state, setState] = useState<State>({
    data: null,
    loading: true,
    error: null,
    transitioning: false,
    transitionError: null,
  });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await getIncident(incidentId);
      setState((prev) => ({ ...prev, data, loading: false, error: null }));
    } catch (error) {
      setState((prev) => ({ ...prev, data: null, loading: false, error: errorMessage(error) }));
    }
  }, [incidentId]);

  useEffect(() => {
    load();
  }, [load]);

  const runTransition = useCallback(
    async (action: () => Promise<unknown>) => {
      setState((prev) => ({ ...prev, transitioning: true, transitionError: null }));
      try {
        await action();
        // The transition endpoints return only the ad-hoc {id, previous_status, status}
        // shape (api/types.ts's own IncidentTransitionResult docstring) - not the full
        // detail - so a real re-fetch is how the screen gets the fresh event timeline.
        await load();
        setState((prev) => ({ ...prev, transitioning: false }));
      } catch (error) {
        setState((prev) => ({ ...prev, transitioning: false, transitionError: errorMessage(error) }));
        throw error;
      }
    },
    [load],
  );

  return {
    ...state,
    refresh: load,
    acknowledge: (reason?: string) => runTransition(() => acknowledgeIncident(incidentId, reason)),
    investigate: (reason?: string) => runTransition(() => investigateIncident(incidentId, reason)),
    resolve: (resolutionCode: string, reason?: string) =>
      runTransition(() => resolveIncident(incidentId, resolutionCode, reason)),
    dismiss: (resolutionCode: string, reason?: string) =>
      runTransition(() => dismissIncident(incidentId, resolutionCode, reason)),
  };
}
