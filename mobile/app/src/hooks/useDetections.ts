import { useCallback, useEffect, useRef, useState } from 'react';
import { listDetections } from '../api/tenant';
import type { DetectionOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

interface State {
  items: DetectionOut[];
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
}

const INITIAL: State = { items: [], nextCursor: null, loading: true, loadingMore: false, error: null };

/** Structural clone of `useIncidents.ts` minus the `status` parameter - a raw detection
 * has no status concept, so there is nothing to filter on and nothing to reset the list
 * for besides an explicit `refresh()`. Same cursor-pagination logic, same `stateRef`
 * re-entrancy guard. */
export function useDetections() {
  const [state, setState] = useState<State>(INITIAL);
  // Mirrors the latest state synchronously for loadMore's own re-entrancy/emptiness
  // guard, which needs to read "is there already a fetch in flight, is there a next
  // page" before its first `await` - state itself only updates on the next render.
  const stateRef = useRef(state);
  stateRef.current = state;

  const load = useCallback(async () => {
    setState(INITIAL);
    try {
      const page = await listDetections();
      setState({ items: page.items, nextCursor: page.next_cursor, loading: false, loadingMore: false, error: null });
    } catch (error) {
      setState({ items: [], nextCursor: null, loading: false, loadingMore: false, error: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const loadMore = useCallback(async () => {
    const current = stateRef.current;
    if (current.loading || current.loadingMore || !current.nextCursor) return;
    setState((prev) => ({ ...prev, loadingMore: true }));
    try {
      const page = await listDetections({ cursor: current.nextCursor });
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
  }, []);

  return { ...state, refresh: load, loadMore };
}
