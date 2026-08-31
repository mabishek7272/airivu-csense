import { useCallback, useEffect, useRef, useState } from 'react';
import { listIncidents } from '../api/tenant';
import type { IncidentStatusFilter, IncidentSummary } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  items: IncidentSummary[];
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
}

const INITIAL: State = { items: [], nextCursor: null, loading: true, loadingMore: false, error: null };

/** Cursor pagination mirrors the (now-deleted, see git history) native Android app's own
 * `IncidentListViewModel`: switching `status` resets the list and cursor from scratch -
 * a filter change is a new query, not a continuation of the old one. */
export function useIncidents(status: IncidentStatusFilter) {
  const [state, setState] = useState<State>(INITIAL);
  // Mirrors the latest state synchronously for loadMore's own re-entrancy/emptiness
  // guard, which needs to read "is there already a fetch in flight, is there a next
  // page" before its first `await` - state itself only updates on the next render.
  const stateRef = useRef(state);
  stateRef.current = state;

  const load = useCallback(async () => {
    setState(INITIAL);
    try {
      const page = await listIncidents({ status });
      setState({ items: page.items, nextCursor: page.next_cursor, loading: false, loadingMore: false, error: null });
    } catch (error) {
      setState({ items: [], nextCursor: null, loading: false, loadingMore: false, error: errorMessage(error) });
    }
  }, [status]);

  useEffect(() => {
    load();
  }, [load]);

  const loadMore = useCallback(async () => {
    const current = stateRef.current;
    if (current.loading || current.loadingMore || !current.nextCursor) return;
    setState((prev) => ({ ...prev, loadingMore: true }));
    try {
      const page = await listIncidents({ status, cursor: current.nextCursor });
      setState((prev) => ({
        items: [...prev.items, ...page.items],
        nextCursor: page.next_cursor,
        loading: false,
        loadingMore: false,
        error: null,
      }));
    } catch (error) {
      setState((prev) => ({ ...prev, loadingMore: false, error: errorMessage(error) }));
    }
  }, [status]);

  return { ...state, refresh: load, loadMore };
}
