import { useCallback, useEffect, useRef, useState } from "react";

/** Loading data, with the states kept distinct.
 *
 *  The important part is what it refuses to conflate. `data === null` while `loading` is
 *  true is *not* "empty" — a view that renders those the same tells an operator there are
 *  no incidents while the request is still in flight, and they stop looking. So `data`
 *  stays null until something has actually arrived, and callers branch on loading first.
 *
 *  A refresh keeps the previous data on screen rather than flashing back to a skeleton.
 *  Re-fetching after a create should not blank the list you just added to.
 */

export interface ResourceState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** True for a background refresh, so the UI can keep showing stale data. */
  refreshing: boolean;
  reload: () => void;
  /** Applies a local change without a round trip — used after a create or delete so the
   *  list updates immediately and the server call confirms it rather than gating it. */
  mutate: (updater: (current: T | null) => T | null) => void;
}

export function useResource<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
): ResourceState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // Guards against a slow first request resolving after a fast second one and overwriting
  // it with stale data — the classic filter race, where clearing a filter briefly shows
  // the old filtered results.
  const requestId = useRef(0);
  const hasData = useRef(false);

  const run = useCallback(async () => {
    const id = ++requestId.current;
    if (hasData.current) setRefreshing(true);
    else setLoading(true);

    try {
      const result = await fetcher();
      if (id !== requestId.current) return;
      setData(result);
      setError(null);
      hasData.current = true;
    } catch (err) {
      if (id !== requestId.current) return;
      setError(err);
      // Deliberately not clearing `data`: if a refresh fails, the previously loaded list
      // is still true and more useful than an empty screen. The caller decides whether to
      // show the error alongside it.
    } finally {
      if (id === requestId.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    void run();
  }, [run]);

  const mutate = useCallback((updater: (current: T | null) => T | null) => {
    setData((current) => updater(current));
  }, []);

  return { data, error, loading, refreshing, reload: () => void run(), mutate };
}
